import io
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any

import cv2
import ddddocr
import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.wait import WebDriverWait

from config import (
    APP_BASE_URL,
    APP_VERSION,
    API_BASE_URL,
    CAPTCHA_SOLVER_URL,
    CAPTCHA_RETRY_LIMIT,
    COOKIE_FILE,
    DEBUG,
    DOWNLOAD_MAX_RETRIES,
    DOWNLOAD_RETRY_DELAY,
    DOWNLOAD_TIMEOUT,
    LINUX,
    MAX_DELAY,
    PAGE_TIMEOUT,
    POINTS_TO_CNY_RATE,
    REQUEST_TIMEOUT,
)

# 自定义异常
class CaptchaRetryableError(Exception):
    """验证码处理过程中可重试的错误"""
    pass

class APILoginError(Exception):
    """API登录失败异常"""
    pass

class CookieLoadError(Exception):
    """Cookie加载失败异常"""
    pass

# 通知模块导入
try:
    from notify import send
    NOTIFY_AVAILABLE = True
except ImportError:
    NOTIFY_AVAILABLE = False
    def send(title, content):
        logger.warning("通知模块不可用")

# 配置专业日志系统
def setup_logger():
    """配置专业的日志系统"""
    logger = logging.getLogger('RainyunAutomation')
    logger.setLevel(logging.INFO)

    # 清除现有处理器
    if logger.hasHandlers():
        logger.handlers.clear()

    # 创建格式化器
    formatter = logging.Formatter(
        '%(asctime)s.%(msecs)03d | %(levelname)-8s | %(module)s.%(funcName)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 创建字符串处理器（用于收集所有日志）
    log_capture_string = io.StringIO()
    string_handler = logging.StreamHandler(log_capture_string)
    string_handler.setFormatter(formatter)
    logger.addHandler(string_handler)

    # 创建控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger, log_capture_string

# 初始化日志
logger, log_capture_string = setup_logger()

API_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

@dataclass
class RuntimeContext:
    """运行时上下文"""
    driver: WebDriver
    wait: WebDriverWait
    ocr: ddddocr.DdddOcr
    det: ddddocr.DdddOcr
    temp_dir: str
    session: Optional[requests.Session] = None
    api_cookies: Optional[Dict[str, str]] = None
    browser_logged_in: bool = False


def build_app_url(path: str) -> str:
    """构建完整的应用URL"""
    return f"{APP_BASE_URL}/{path.lstrip('/')}"


def build_api_url(path: str) -> str:
    """构建完整的API URL"""
    return f"{API_BASE_URL}/{path.lstrip('/')}"


def build_captcha_solver_url() -> str:
    """构建验证码识别接口地址"""
    if not CAPTCHA_SOLVER_URL:
        return ""
    if "solve_captcha" in CAPTCHA_SOLVER_URL:
        return CAPTCHA_SOLVER_URL
    return f"{CAPTCHA_SOLVER_URL}/solve_captcha?aid=2039519451&type=1"


def temp_path(ctx: RuntimeContext, filename: str) -> str:
    """获取临时文件路径"""
    return os.path.join(ctx.temp_dir, filename)


def clear_temp_dir(temp_dir: str) -> None:
    """清理临时目录"""
    if not os.path.exists(temp_dir):
        return
    for filename in os.listdir(temp_dir):
        file_path = os.path.join(temp_dir, filename)
        if os.path.isfile(file_path) or os.path.islink(file_path):
            try:
                os.remove(file_path)
            except OSError as e:
                logger.warning(f"删除临时文件失败 {file_path}: {e}")


# ==================== API快速登录核心功能 ====================

def api_login_with_requests(user: str, pwd: str) -> Dict[str, Any]:
    """
    通过API快速登录雨云，返回登录状态和Cookie

    Args:
        user: 用户名
        pwd: 密码

    Returns:
        Dict: 包含登录结果、Cookie和session的字典

    Raises:
        APILoginError: API登录失败时抛出
    """
    logger.info("开始API快速登录流程")

    session = requests.Session()
    start_time = time.time()

    # 配置请求头
    headers = {
        'User-Agent': API_USER_AGENT,
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/json',
        'Origin': 'https://v2.rainyun.com',
        'Referer': 'https://v2.rainyun.com/',
    }

    # 登录数据
    login_data = {
        "field": user,
        "password": pwd
    }

    try:
        login_url = build_api_url("/user/login")
        logger.debug(f"发送登录请求到: {login_url}")
        logger.debug(f"登录数据: {login_data}")

        response = session.post(
            login_url,
            json=login_data,
            headers=headers,
            timeout=30
        )

        elapsed_time = (time.time() - start_time) * 1000
        logger.debug(f"API响应时间: {elapsed_time:.2f}ms")
        logger.debug(f"响应状态码: {response.status_code}")
        logger.debug(f"响应头: {dict(response.headers)}")

        if response.status_code == 200:
            response_data = response.json()
            logger.info(f"API登录成功，耗时: {elapsed_time:.2f}ms")

            # 获取Cookie
            cookies = session.cookies.get_dict()
            logger.info(f"获取到 {len(cookies)} 个会话Cookie")

            # 验证响应数据
            if 'token' in response_data or 'data' in response_data or 'access_token' in response_data:
                logger.debug("响应中包含有效凭证")
            else:
                logger.warning("响应中未找到标准凭证字段")

            return {
                'success': True,
                'cookies': cookies,
                'session': session,
                'response_data': response_data,
                'elapsed_ms': elapsed_time
            }
        else:
            error_msg = f"API登录失败: {response.status_code}"
            try:
                error_data = response.json()
                error_msg += f" | 错误信息: {error_data}"
            except:
                error_msg += f" | 原始响应: {response.text[:200]}"

            logger.error(error_msg)
            raise APILoginError(error_msg)

    except requests.exceptions.Timeout:
        error_msg = "API登录请求超时"
        logger.error(error_msg)
        raise APILoginError(error_msg)
    except requests.exceptions.ConnectionError as e:
        error_msg = f"API连接失败: {e}"
        logger.error(error_msg)
        raise APILoginError(error_msg)
    except Exception as e:
        error_msg = f"API登录异常: {type(e).__name__}: {e}"
        logger.error(error_msg)
        raise APILoginError(error_msg)


def get_session_cookie_header(session: requests.Session) -> str:
    """将Session Cookie转换为请求头字符串"""
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in session.cookies)


def get_csrf_token(session: requests.Session) -> Optional[str]:
    """从Session Cookie中提取雨云CSRF Token"""
    for cookie in session.cookies:
        if cookie.name.lower() == "x-csrf-token":
            return cookie.value
    return None


def build_api_headers(session: requests.Session, include_content_type: bool = True) -> Dict[str, str]:
    """构建雨云API请求头"""
    headers = {
        "User-Agent": API_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://v2.rainyun.com",
        "Referer": "https://v2.rainyun.com/",
    }

    if include_content_type:
        headers["Content-Type"] = "application/json"

    csrf_token = get_csrf_token(session)
    if csrf_token:
        headers["x-csrf-token"] = csrf_token

    cookie_header = get_session_cookie_header(session)
    if cookie_header:
        headers["Cookie"] = cookie_header

    return headers


def get_slide_verify(session: requests.Session) -> tuple[Optional[str], Optional[str]]:
    """通过第三方接口获取腾讯验证码ticket和randstr"""
    solver_url = build_captcha_solver_url()
    if not solver_url:
        logger.warning("未配置 CAPTCHA_SOLVER_URL，跳过API验证码获取")
        return None, None

    for attempt in range(1, CAPTCHA_RETRY_LIMIT + 1):
        try:
            logger.info(f"请求验证码识别接口 (尝试 {attempt}/{CAPTCHA_RETRY_LIMIT}): {solver_url}")
            response = session.get(
                solver_url,
                headers={
                    "User-Agent": API_USER_AGENT,
                    "Content-Type": "application/json",
                },
                timeout=REQUEST_TIMEOUT,
            )

            try:
                result = response.json()
            except ValueError:
                logger.warning(f"验证码接口返回非JSON: HTTP {response.status_code} {response.text[:200]}")
                result = {}

            message = str(result.get("message") or result.get("msg") or "")
            if str(result.get("code")) == "200" and message.lower() == "success":
                data = result.get("data") or result.get("Data") or {}
                ticket = data.get("ticket")
                randstr = data.get("randstr")
                if ticket and randstr:
                    logger.info("验证码ticket获取成功")
                    return ticket, randstr

            logger.warning(
                f"验证码接口返回异常: HTTP {response.status_code}, "
                f"code={result.get('code')}, message={message}"
            )

        except requests.RequestException as e:
            logger.warning(f"验证码接口请求失败 (尝试 {attempt}): {e}")

        if attempt < CAPTCHA_RETRY_LIMIT:
            time.sleep(2)

    return None, None


def get_api_user_info(session: requests.Session) -> Optional[Dict[str, Any]]:
    """通过雨云API获取用户信息"""
    csrf_token = get_csrf_token(session)
    if not csrf_token:
        logger.warning("未找到 X-CSRF-Token，无法获取用户信息")
        return None

    try:
        response = session.get(
            build_api_url("/user/?no_cache=false"),
            headers=build_api_headers(session),
            timeout=REQUEST_TIMEOUT,
        )

        try:
            result = response.json()
        except ValueError:
            logger.warning(f"获取用户信息返回非JSON: HTTP {response.status_code} {response.text[:200]}")
            return None

        data = result.get("data") or result.get("Data") or {}
        if not data:
            logger.warning(
                f"获取用户信息失败: HTTP {response.status_code}, "
                f"code={result.get('code')}, message={result.get('message')}"
            )
            return None

        return {
            "name": data.get("Name"),
            "email": data.get("Email"),
            "points": data.get("Points"),
            "last_ip": data.get("LastIP"),
            "last_login_area": data.get("LastLoginArea"),
        }

    except requests.RequestException as e:
        logger.warning(f"获取用户信息请求失败: {e}")
        return None


def log_points_from_user_info(user_info: Optional[Dict[str, Any]], prefix: str = "当前积分") -> None:
    """记录用户积分信息"""
    if not user_info:
        return

    points = user_info.get("points")
    if points is None:
        return

    try:
        current_points = int(points)
        cny_value = current_points / POINTS_TO_CNY_RATE
        logger.info(f"{prefix}: {current_points:,} | 约 ¥{cny_value:.2f} 元")
    except (TypeError, ValueError):
        logger.info(f"{prefix}: {points}")


def api_checkin_with_session(session: Optional[requests.Session]) -> bool:
    """参考ql.js，通过API验证码ticket完成每日签到"""
    if not session:
        logger.info("无API会话，跳过API签到")
        return False

    csrf_token = get_csrf_token(session)
    if not csrf_token:
        logger.warning("API会话缺少 X-CSRF-Token，跳过API签到")
        return False

    before_info = get_api_user_info(session)
    ticket, randstr = get_slide_verify(session)
    if not ticket or not randstr:
        logger.error("获取验证码ticket失败")
        return False

    payload = {
        "task_name": "每日签到",
        "verifyCode": "",
        "vticket": ticket,
        "vrandstr": randstr,
    }

    try:
        logger.info("提交API签到请求")
        response = session.post(
            build_api_url("/user/reward/tasks"),
            json=payload,
            headers=build_api_headers(session),
            timeout=REQUEST_TIMEOUT,
        )

        try:
            result = response.json()
        except ValueError:
            logger.warning(f"API签到返回非JSON: HTTP {response.status_code} {response.text[:200]}")
            return False

        code = result.get("code")
        message = result.get("message") or result.get("msg") or ""
        already_signed = any(pattern in message for pattern in ["已签到", "已领取", "已完成", "明日再来"])

        if str(code) == "200":
            logger.info(message or "API签到成功")
        elif already_signed:
            logger.info(f"今日已签到: {message}")
        else:
            logger.warning(f"API签到失败: HTTP {response.status_code}, code={code}, message={message}")
            return False

        after_info = get_api_user_info(session) or before_info
        log_points_from_user_info(after_info, "签到后积分")
        logger.info("━━━━━━ 任务执行完成 ━━━━━━")
        return True

    except requests.RequestException as e:
        logger.warning(f"API签到请求失败: {e}")
        return False


def api_checkin(ctx: RuntimeContext) -> bool:
    """使用运行时上下文中的API会话完成每日签到"""
    return api_checkin_with_session(ctx.session)


def api_login_and_checkin(user: str, pwd: str) -> bool:
    """不启动浏览器，直接使用API登录并签到"""
    try:
        api_result = api_login_with_requests(user, pwd)
    except APILoginError as e:
        logger.warning(f"API直连登录失败: {e}")
        return False
    except Exception as e:
        logger.warning(f"API直连流程异常: {type(e).__name__}: {e}")
        return False

    if not api_result.get("success"):
        logger.warning("API直连登录未返回成功状态")
        return False

    return api_checkin_with_session(api_result.get("session"))


def load_api_cookies_to_browser(driver: WebDriver, cookies: Dict[str, str], target_url: str) -> bool:
    """
    将API获取的Cookie加载到Selenium浏览器

    Args:
        driver: Selenium WebDriver实例
        cookies: API获取的Cookie字典
        target_url: 目标URL

    Returns:
        bool: Cookie加载是否成功
    """
    logger.info("开始加载API Cookie到浏览器")

    try:
        # 步骤1: 访问目标网站
        logger.debug(f"访问目标URL: {target_url}")
        driver.get("about:blank")  # 先清空
        driver.get(target_url)
        time.sleep(2)

        # 步骤2: 清除浏览器现有Cookie
        logger.debug("清除浏览器现有Cookie")
        driver.delete_all_cookies()
        time.sleep(1)

        # 步骤3: 解析域名
        domain = target_url.split('/')[2]
        logger.debug(f"目标域名: {domain}")

        # 步骤4: 添加Cookie
        added_count = 0
        for name, value in cookies.items():
            try:
                cookie_config = {
                    'name': name,
                    'value': str(value),
                    'domain': domain,
                    'path': '/',
                }
                driver.add_cookie(cookie_config)
                added_count += 1
                logger.debug(f"添加Cookie: {name}")
            except Exception as e:
                logger.warning(f"添加Cookie {name} 失败: {e}")
                # 尝试使用更通用的domain
                try:
                    cookie_config['domain'] = '.' + domain
                    driver.add_cookie(cookie_config)
                    added_count += 1
                    logger.debug(f"使用泛域名添加Cookie: {name}")
                except:
                    pass

        logger.info(f"成功添加 {added_count}/{len(cookies)} 个Cookie")

        # 步骤5: 刷新页面使Cookie生效
        logger.debug("刷新页面使Cookie生效")
        driver.refresh()
        time.sleep(3)

        # 步骤6: 验证登录状态
        current_url = driver.current_url
        page_source = driver.page_source

        login_indicators = ['登录', 'sign in', 'login']
        is_logged_in = True

        for indicator in login_indicators:
            if indicator.lower() in page_source.lower():
                is_logged_in = False
                break

        if is_logged_in or 'dashboard' in current_url:
            logger.info("✅ Cookie加载成功，浏览器已保持登录状态")
            return True
        else:
            logger.warning("Cookie可能无效或已过期")
            return False

    except Exception as e:
        logger.error(f"加载Cookie到浏览器失败: {type(e).__name__}: {e}")
        return False


def hybrid_login_flow(ctx: RuntimeContext, user: str, pwd: str) -> bool:
    """
    混合登录流程：优先使用API快速登录，失败时回退到浏览器登录

    Args:
        ctx: 运行时上下文
        user: 用户名
        pwd: 密码

    Returns:
        bool: 登录是否成功
    """
    logger.info("开始混合登录流程")
    login_start_time = time.time()

    # 第一阶段：尝试API快速登录
    logger.info("阶段1: 尝试API快速登录")
    try:
        api_result = api_login_with_requests(user, pwd)

        if api_result['success']:
            logger.info("API快速登录成功，开始注入Cookie到浏览器")
            ctx.api_cookies = api_result['cookies']
            ctx.session = api_result['session']

            # 注入Cookie到浏览器
            if load_api_cookies_to_browser(ctx.driver, api_result['cookies'], build_app_url("/dashboard")):
                login_elapsed = (time.time() - login_start_time) * 1000
                ctx.browser_logged_in = True
                logger.info(f"✅ 混合登录成功，总耗时: {login_elapsed:.2f}ms")
                return True
            else:
                login_elapsed = (time.time() - login_start_time) * 1000
                logger.warning("Cookie注入失败，将优先使用API签到，必要时再回退浏览器登录")
                logger.info(f"API登录可用，总耗时: {login_elapsed:.2f}ms")
                return True
        else:
            logger.warning("API登录未返回成功状态，继续第二阶段")

    except APILoginError as e:
        logger.warning(f"API登录失败: {e}，继续第二阶段")
    except Exception as e:
        logger.error(f"API登录流程异常: {type(e).__name__}: {e}，继续第二阶段")

    # 第二阶段：回退到浏览器登录
    logger.info("阶段2: 回退到浏览器登录")
    try:
        if do_browser_login(ctx, user, pwd):
            login_elapsed = (time.time() - login_start_time) * 1000
            ctx.browser_logged_in = True
            logger.info(f"✅ 浏览器登录成功，总耗时: {login_elapsed:.2f}ms")
            return True
        else:
            logger.error("浏览器登录失败")
            return False
    except Exception as e:
        logger.error(f"浏览器登录异常: {type(e).__name__}: {e}")
        return False


def do_browser_login(ctx: RuntimeContext, user: str, pwd: str) -> bool:
    """
    浏览器登录流程

    Args:
        ctx: 运行时上下文
        user: 用户名
        pwd: 密码

    Returns:
        bool: 登录是否成功
    """
    logger.info("执行浏览器登录流程")
    login_url = build_app_url("/auth/login")

    try:
        # 访问登录页面
        logger.debug(f"访问登录页面: {login_url}")
        ctx.driver.get(login_url)

        # 等待并填写表单
        logger.debug("等待登录表单加载")
        username_field = ctx.wait.until(EC.visibility_of_element_located((By.NAME, 'login-field')))
        password_field = ctx.wait.until(EC.visibility_of_element_located((By.NAME, 'login-password')))
        login_button = ctx.wait.until(EC.visibility_of_element_located(
            (By.XPATH, '//*[@id="app"]/div[1]/div[1]/div/div[2]/fade/div/div/span/form/button')
        ))

        # 填写表单
        logger.debug("填写登录表单")
        username_field.send_keys(user)
        password_field.send_keys(pwd)
        login_button.click()

        # 处理验证码（如果存在）
        try:
            logger.debug("检查验证码")
            ctx.wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, 'tcaptcha_iframe_dy')))
            logger.info("检测到验证码，开始处理")

            if not process_captcha(ctx):
                logger.error("验证码处理失败")
                return False

        except TimeoutException:
            logger.debug("未检测到验证码")

        # 切换回默认内容
        ctx.driver.switch_to.default_content()
        time.sleep(2)

        # 验证登录成功
        logger.debug("验证登录状态")
        ctx.wait.until(EC.url_contains("dashboard"))

        # 验证用户信息
        current_url = ctx.driver.current_url
        logger.info(f"登录成功，当前URL: {current_url}")

        return True

    except TimeoutException as e:
        logger.error(f"浏览器登录超时: {e}")
        return False
    except Exception as e:
        logger.error(f"浏览器登录异常: {type(e).__name__}: {e}")
        return False


def init_selenium(debug: bool, linux: bool) -> WebDriver:
    """初始化Selenium WebDriver"""
    logger.debug("初始化Selenium WebDriver")

    options = Options()

    # 基础配置
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option('useAutomationExtension', False)

    if debug:
        options.add_experimental_option("detach", True)
        logger.debug("启用调试模式")

    if linux:
        options.add_argument("--headless")
        options.add_argument("--disable-gpu")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        logger.debug("Linux环境配置: 无头模式")

        # 二进制路径
        chrome_bin = os.environ.get("CHROME_BIN")
        if chrome_bin and os.path.exists(chrome_bin):
            options.binary_location = chrome_bin
            logger.debug(f"使用自定义Chrome二进制: {chrome_bin}")

        # Driver路径
        chromedriver_path = os.environ.get("CHROMEDRIVER_PATH", "/usr/local/share/chromedriver-linux64/chromedriver")
        if os.path.exists(chromedriver_path):
            logger.debug(f"使用系统chromedriver: {chromedriver_path}")
            return webdriver.Chrome(service=Service(chromedriver_path), options=options)
        else:
            logger.debug("使用本地chromedriver")
            return webdriver.Chrome(service=Service("./chromedriver"), options=options)
    else:
        logger.debug("Windows环境配置")
        return webdriver.Chrome(service=Service("chromedriver.exe"), options=options)


def download_image(url: str, output_path: str) -> bool:
    """下载图片到本地"""
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    for attempt in range(1, DOWNLOAD_MAX_RETRIES + 1):
        try:
            logger.debug(f"下载图片 (尝试 {attempt}/{DOWNLOAD_MAX_RETRIES}): {url}")

            response = requests.get(url, timeout=DOWNLOAD_TIMEOUT, stream=True)
            response.raise_for_status()

            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            file_size = os.path.getsize(output_path)
            logger.debug(f"图片下载成功: {output_path} ({file_size} bytes)")
            return True

        except requests.RequestException as e:
            logger.warning(f"下载图片失败 (尝试 {attempt}): {e}")
            if attempt < DOWNLOAD_MAX_RETRIES:
                time.sleep(DOWNLOAD_RETRY_DELAY)

    logger.error(f"图片下载失败，已重试 {DOWNLOAD_MAX_RETRIES} 次: {url}")
    return False


def get_url_from_style(style: str) -> str:
    """从style属性中提取URL"""
    if not style:
        raise ValueError("style属性为空")

    match = re.search(r"url\(([^)]+)\)", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从style中解析URL: {style}")

    url = match.group(1).strip().strip('"').strip("'")
    return url


def get_width_from_style(style: str) -> float:
    """从style属性中提取宽度"""
    if not style:
        raise ValueError("style属性为空")

    match = re.search(r"width\s*:\s*([\d.]+)px", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从style中解析宽度: {style}")

    return float(match.group(1))


def get_height_from_style(style: str) -> float:
    """从style属性中提取高度"""
    if not style:
        raise ValueError("style属性为空")

    match = re.search(r"height\s*:\s*([\d.]+)px", style, re.IGNORECASE)
    if not match:
        raise ValueError(f"无法从style中解析高度: {style}")

    return float(match.group(1))


def get_element_size(element) -> tuple[float, float]:
    """获取元素尺寸"""
    size = element.size or {}
    width = size.get("width", 0)
    height = size.get("height", 0)

    if not width or not height:
        raise ValueError("无法从元素尺寸解析宽高")

    return float(width), float(height)


def find_visible_element(driver: WebDriver, by: str, value: str):
    """查找第一个可见且可用的元素"""
    for element in driver.find_elements(by, value):
        if element.is_displayed() and element.is_enabled():
            return element
    return False


def refresh_captcha(ctx: RuntimeContext) -> bool:
    """刷新当前验证码，避免点击隐藏的reload节点"""
    try:
        reload_btn = ctx.wait.until(lambda driver: find_visible_element(driver, By.XPATH, '//*[@id="reload"]'))
        ctx.driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'center'});", reload_btn)
        time.sleep(0.5)

        try:
            reload_btn.click()
        except Exception:
            ctx.driver.execute_script("arguments[0].click();", reload_btn)

        time.sleep(2)
        return True
    except Exception as e:
        logger.error(f"无法刷新验证码: {e}")
        return False


def process_captcha(ctx: RuntimeContext, retry_count: int = 0) -> bool:
    """处理验证码"""
    if retry_count >= CAPTCHA_RETRY_LIMIT:
        logger.error(f"验证码重试次数过多 ({CAPTCHA_RETRY_LIMIT} 次)，任务失败")
        return False

    try:
        logger.info(f"开始处理验证码 (尝试 {retry_count + 1}/{CAPTCHA_RETRY_LIMIT})")
        download_captcha_img(ctx)

        if check_captcha(ctx):
            logger.info("开始识别验证码")
            captcha = cv2.imread(temp_path(ctx, "captcha.jpg"))

            if captcha is None:
                logger.error("验证码背景图读取失败")
                raise CaptchaRetryableError("验证码图片读取失败")

            with open(temp_path(ctx, "captcha.jpg"), 'rb') as f:
                captcha_b = f.read()

            bboxes = ctx.det.detection(captcha_b)
            result = dict()

            for i in range(len(bboxes)):
                x1, y1, x2, y2 = bboxes[i]
                spec = captcha[y1:y2, x1:x2]
                cv2.imwrite(temp_path(ctx, f"spec_{i + 1}.jpg"), spec)

                for j in range(3):
                    similarity, matched = compute_similarity(
                        temp_path(ctx, f"sprite_{j + 1}.jpg"),
                        temp_path(ctx, f"spec_{i + 1}.jpg")
                    )

                    similarity_key = f"sprite_{j + 1}.similarity"
                    position_key = f"sprite_{j + 1}.position"

                    if similarity_key in result.keys():
                        if float(result[similarity_key]) < similarity:
                            result[similarity_key] = similarity
                            result[position_key] = f"{int((x1 + x2) / 2)},{int((y1 + y2) / 2)}"
                    else:
                        result[similarity_key] = similarity
                        result[position_key] = f"{int((x1 + x2) / 2)},{int((y1 + y2) / 2)}"

            if check_answer(result):
                for i in range(3):
                    similarity_key = f"sprite_{i + 1}.similarity"
                    position_key = f"sprite_{i + 1}.position"
                    position = result[position_key]
                    logger.info(f"图案 {i + 1} 位于 ({position})，匹配率: {result[similarity_key]:.3f}")

                    slide_bg = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]')))
                    style = slide_bg.get_attribute("style")

                    x, y = int(position.split(",")[0]), int(position.split(",")[1])
                    width_raw, height_raw = captcha.shape[1], captcha.shape[0]

                    try:
                        width = get_width_from_style(style)
                        height = get_height_from_style(style)
                    except ValueError:
                        width, height = get_element_size(slide_bg)

                    x_offset, y_offset = float(-width / 2), float(-height / 2)
                    final_x = int(x_offset + x / width_raw * width)
                    final_y = int(y_offset + y / height_raw * height)

                    ActionChains(ctx.driver).move_to_element_with_offset(slide_bg, final_x, final_y).click().perform()

                confirm = ctx.wait.until(
                    EC.element_to_be_clickable((By.XPATH, '//*[@id="tcStatus"]/div[2]/div[2]/div/div'))
                )
                logger.info("提交验证码")
                confirm.click()
                time.sleep(5)

                result_el = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="tcOperation"]')))
                if 'show-success' in result_el.get_attribute("class"):
                    logger.info("验证码通过")
                    return True
                else:
                    logger.error("验证码未通过")
            else:
                logger.error("验证码识别失败")
        else:
            logger.error("当前验证码识别率低")

        # 刷新验证码重试
        if not refresh_captcha(ctx):
            return False

        return process_captcha(ctx, retry_count + 1)

    except (TimeoutException, ValueError, CaptchaRetryableError) as e:
        logger.error(f"验证码处理异常: {type(e).__name__}: {e}")

        if refresh_captcha(ctx):
            return process_captcha(ctx, retry_count + 1)
        else:
            return False


def download_captcha_img(ctx: RuntimeContext):
    """下载验证码图片"""
    clear_temp_dir(ctx.temp_dir)

    slide_bg = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="slideBg"]')))
    img1_style = slide_bg.get_attribute("style")
    img1_url = get_url_from_style(img1_style)

    logger.info(f"下载验证码背景图: {img1_url}")
    if not download_image(img1_url, temp_path(ctx, "captcha.jpg")):
        raise CaptchaRetryableError("验证码背景图下载失败")

    sprite = ctx.wait.until(EC.visibility_of_element_located((By.XPATH, '//*[@id="instruction"]/div/img')))
    img2_url = sprite.get_attribute("src")

    logger.info(f"下载验证码小图: {img2_url}")
    if not download_image(img2_url, temp_path(ctx, "sprite.jpg")):
        raise CaptchaRetryableError("验证码小图下载失败")


def check_captcha(ctx: RuntimeContext) -> bool:
    """检查验证码质量"""
    raw = cv2.imread(temp_path(ctx, "sprite.jpg"))

    if raw is None:
        logger.error("验证码小图读取失败")
        return False

    for i in range(3):
        w = raw.shape[1]
        temp = raw[:, w // 3 * i: w // 3 * (i + 1)]
        cv2.imwrite(temp_path(ctx, f"sprite_{i + 1}.jpg"), temp)

        with open(temp_path(ctx, f"sprite_{i + 1}.jpg"), mode="rb") as f:
            temp_rb = f.read()

        if ctx.ocr.classification(temp_rb) in ["0", "1"]:
            return False

    return True


def check_answer(d: dict) -> bool:
    """检查验证码识别结果"""
    if not d or len(d) < 6:
        logger.warning(f"验证码识别结果不完整，期望6个键，实际{len(d)}个")
        return False

    flipped = dict()
    for key in d.keys():
        flipped[d[key]] = key

    return len(d.values()) == len(flipped.keys())


def compute_similarity(img1_path, img2_path):
    """计算图片相似度"""
    img1 = cv2.imread(img1_path, cv2.IMREAD_GRAYSCALE)
    img2 = cv2.imread(img2_path, cv2.IMREAD_GRAYSCALE)

    sift = cv2.SIFT_create()
    kp1, des1 = sift.detectAndCompute(img1, None)
    kp2, des2 = sift.detectAndCompute(img2, None)

    if des1 is None or des2 is None:
        return 0.0, 0

    bf = cv2.BFMatcher()
    matches = bf.knnMatch(des1, des2, k=2)

    good = [m for m_n in matches if len(m_n) == 2 for m, n in [m_n] if m.distance < 0.8 * n.distance]

    if len(good) == 0:
        return 0.0, 0

    similarity = len(good) / len(matches)
    return similarity, len(good)


def run():
    """主运行函数"""
    ctx = None
    driver = None
    temp_dir = None
    execution_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        # 执行ID
        logger.info(f"执行ID: {execution_id}")
        logger.info(f"━━━━━━ 雨云自动化签到 v{APP_VERSION} ━━━━━━")

        # 读取配置
        timeout = PAGE_TIMEOUT
        max_delay = MAX_DELAY
        user = os.environ.get("RAINYUN_USER", "")
        pwd = os.environ.get("RAINYUN_PWD", "")
        debug = DEBUG
        linux = LINUX

        # 验证配置
        if not user or not pwd:
            logger.critical("环境变量 RAINYUN_USER 和 RAINYUN_PWD 未设置")
            raise ValueError("缺少必要的登录凭据")

        logger.info(f"用户名: {user}")
        logger.info(f"执行环境: {'Linux' if linux else 'Windows'}")
        logger.info(f"超时设置: {timeout}s")
        logger.info(f"调试模式: {'开启' if debug else '关闭'}")

        # 随机延迟
        # delay = random.randint(0, max_delay)
        # delay_sec = random.randint(0, 60)
        # logger.debug(f"随机延迟: {delay}m {delay_sec}s")
        # time.sleep(delay * 60 + delay_sec)

        logger.info("━━━━━━ 尝试API直连签到 ━━━━━━")
        if api_login_and_checkin(user, pwd):
            return

        logger.warning("API直连签到未完成，初始化浏览器回退流程")

        # 初始化组件
        logger.info("初始化组件")

        logger.debug("初始化ddddocr")
        ocr = ddddocr.DdddOcr(ocr=True, show_ad=False)
        det = ddddocr.DdddOcr(det=True, show_ad=False)

        logger.debug("初始化Selenium WebDriver")
        driver = init_selenium(debug=debug, linux=linux)

        # 反检测
        logger.debug("配置反检测")
        with open("stealth.min.js", mode="r") as f:
            js = f.read()
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": js})

        wait = WebDriverWait(driver, timeout)
        temp_dir = tempfile.mkdtemp(prefix=f"rainyun_{execution_id}_")

        ctx = RuntimeContext(
            driver=driver,
            wait=wait,
            ocr=ocr,
            det=det,
            temp_dir=temp_dir
        )

        logger.info("━━━━━━ 开始登录流程 ━━━━━━")

        # 使用混合登录流程
        logged_in = hybrid_login_flow(ctx, user, pwd)
        if not logged_in:
            logger.error("登录流程失败，任务终止")
            raise Exception("登录失败")

        logger.info("━━━━━━ 开始签到流程 ━━━━━━")
        if api_checkin(ctx):
            return

        logger.warning("API签到未完成，回退到浏览器页面签到流程")
        if not ctx.browser_logged_in:
            logger.info("浏览器会话尚未登录，执行浏览器登录以继续回退流程")
            if not do_browser_login(ctx, user, pwd):
                logger.error("浏览器登录失败，无法继续页面签到")
                raise Exception("浏览器登录失败")
            ctx.browser_logged_in = True

        # 跳转到赚取积分页面
        earn_url = build_app_url("/account/reward/earn")
        logger.info(f"访问赚取积分页面: {earn_url}")
        ctx.driver.get(earn_url)
        time.sleep(3)

        # 检查签到状态
        try:
            earn_button = wait.until(EC.presence_of_element_located(
                (By.XPATH, "//span[contains(text(), '每日签到')]/ancestor::div[1]//a[contains(text(), '领取奖励')]")
            ))
            logger.info("找到签到按钮，开始签到")
            earn_button.click()
        except TimeoutException:
            # 检查是否已签到
            already_signed_patterns = ['已领取', '已完成', '已签到', '明日再来']
            page_source = ctx.driver.page_source

            for pattern in already_signed_patterns:
                if pattern in page_source:
                    logger.info(f"今日已签到 (检测到: '{pattern}')")
                    logger.info("跳过签到流程，获取积分信息")

                    try:
                        points_element = wait.until(EC.visibility_of_element_located(
                            (By.XPATH, '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3')
                        ))
                        points_text = points_element.get_attribute("textContent")
                        current_points = int(''.join(re.findall(r'\d+', points_text)))
                        cny_value = current_points / POINTS_TO_CNY_RATE
                        logger.info(f"当前积分: {current_points:,} | 约 ¥{cny_value:.2f} 元")
                    except Exception as e:
                        logger.warning(f"获取积分信息失败: {e}")

                    return

        # 处理签到验证码
        logger.info("处理签到验证码")
        try:
            ctx.wait.until(EC.frame_to_be_available_and_switch_to_it((By.ID, 'tcaptcha_iframe_dy')))
            if not process_captcha(ctx):
                logger.error("验证码处理失败")
                raise Exception("签到验证码失败")
        except TimeoutException:
            logger.warning("未找到验证码iframe，可能不需要验证码")
        finally:
            ctx.driver.switch_to.default_content()

        # 等待签到完成
        time.sleep(3)

        # 获取积分信息
        try:
            points_element = wait.until(EC.visibility_of_element_located(
                (By.XPATH, '//*[@id="app"]/div[1]/div[3]/div[2]/div/div/div[2]/div[1]/div[1]/div/p/div/h3')
            ))
            points_text = points_element.get_attribute("textContent")
            current_points = int(''.join(re.findall(r'\d+', points_text)))
            cny_value = current_points / POINTS_TO_CNY_RATE

            logger.info(f"签到成功！当前积分: {current_points:,} | 约 ¥{cny_value:.2f} 元")
            logger.info("━━━━━━ 任务执行完成 ━━━━━━")

        except Exception as e:
            logger.warning(f"获取积分信息失败: {e}")
            logger.info("任务执行完成")

    except Exception as e:
        logger.critical(f"任务执行失败: {type(e).__name__}: {e}")

    finally:
        # 清理资源
        logger.info("开始清理资源")

        if driver:
            try:
                logger.debug("关闭WebDriver")
                driver.quit()
            except Exception as e:
                logger.warning(f"关闭WebDriver失败: {e}")

        if temp_dir and not debug:
            try:
                logger.debug(f"清理临时目录: {temp_dir}")
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"清理临时目录失败: {e}")

        # 发送通知
        if NOTIFY_AVAILABLE:
            try:
                log_content = log_capture_string.getvalue()
                send(f"雨云签到 - {execution_id}", log_content)
                logger.info("通知发送完成")
            except Exception as e:
                logger.warning(f"发送通知失败: {e}")

        # 关闭日志收集器
        log_capture_string.close()


if __name__ == "__main__":
    run()
