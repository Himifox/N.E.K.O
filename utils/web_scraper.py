"""
网络爬虫与上下文感知模块 (Web Scraper & Context Module)
=========================================================

用于异步获取各大平台的热门内容与用户个性化动态，并提供基于桌面环境的智能搜索辅助。

区域化内容分发 (Regional Content)
   - 中文生态：B站 (首页推荐)、微博 (热搜榜)
   - 海外生态：Reddit (热门帖子)、Twitter (趋势话题)

个性化内容获取 (Personalized Content)[需要用户提供Cookie等认证信息]
   - 个性化生态: 个人动态 (B站)、个人关注流 (微博)、个人订阅流 (Reddit)、个人时间线 (Twitter)
   - 动态解析：精准提取视频、图文、专栏文章以及直播开播状态。
"""

import re
import os
import json
import httpx
import asyncio
import random
import string
import platform
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional, Union
from urllib.parse import quote, urljoin, urlparse, parse_qs
from bs4 import BeautifulSoup

# 第三方库
from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage

# ==========================================
# 区域检测与基础配置 
# ==========================================
try:
    from utils.language_utils import is_china_region
except ImportError:
    # 如果 language_utils 不可用，使用回退方案
    import locale
    def is_china_region() -> bool:
        """
        区域检测回退方案

        仅对中国大陆地区返回True（zh_cn及其变体）
        港澳台地区（zh_tw, zh_hk）返回False
        Windows 中文系统返回 True
        """
        mainland_china_locales = {'zh_cn', 'chinese_china', 'chinese_simplified_china'}
        
        def normalize_locale(loc: str) -> str:
            """标准化locale字符串：小写、替换连字符、去除编码"""
            if not loc:
                return ''
            loc = loc.lower()
            loc = loc.replace('-', '_')
            if '.' in loc:
                loc = loc.split('.')[0]
            return loc

        def check_locale(loc: str) -> bool:
            """检查标准化后的locale是否为中国大陆"""
            normalized = normalize_locale(loc)
            if not normalized:
                return False
            if normalized in mainland_china_locales:
                return True
            if normalized.startswith('zh_cn'):
                return True
            if 'chinese' in normalized and 'china' in normalized:
                return True
            return False

      # 尝试 Python 3.11 推荐方法 (getlocale)，并进行安全的异常处理
        try:
            system_locale = locale.getlocale()[0]
            if system_locale and check_locale(system_locale):
                return True
        except ValueError:
            # 预期内异常：getlocale() 遇到不规范的环境变量 (如无效的 LANG) 时会抛出 ValueError，安全放行
            pass
        except Exception as e:
            # 意料之外的异常：记录到 debug 日志，不影响主流程，但保留追溯线索
            logger.debug(f"区域检测 (system_locale) 发生未知异常: {e}")

        # 尝试废弃的备用方法 (getdefaultlocale)，并进行同样的安全处理
        try:
            default_locale = getattr(locale, 'getdefaultlocale', lambda: (None,))()[0]
            if default_locale and check_locale(default_locale):
                return True
        except ValueError:
            pass
        except Exception as e:
            logger.debug(f"区域检测 (default_locale) 发生未知异常: {e}")

        return False

# 使用标准的模块级别 Logger
logger = logging.getLogger(__name__)

# User-Agent池，随机选择以避免被识别
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
]

def get_random_user_agent() -> str:
    """随机获取一个User-Agent"""
    return random.choice(USER_AGENTS)

# ==========================================
# 凭证与 Cookie 获取模块
# ==========================================
def _get_platform_cookies(platform_name: str) -> dict[str, str]:
    """
    通用平台 Cookie 读取器 (接入系统底层的加密/明文统一读取逻辑)
    """
    try:
        # 优先调用系统底层的解密读取逻辑
        from utils.cookies_login import load_cookies_from_file
        cookies = load_cookies_from_file(platform_name)
        if cookies:
            logger.debug(f"✅ 成功通过底层接口加载 {platform_name} 凭证")
            return cookies
    except Exception as e:
        logger.debug(f"底层接口加载 {platform_name} 凭证失败: {e}，尝试使用明文回退...")

    # 下面是作为回退的明文读取逻辑（兜底处理旧文件）
    possible_paths = [
        Path(os.path.expanduser('~')) / f'{platform_name}_cookies.json',
        Path('config') / f'{platform_name}_cookies.json',
        Path('.') / f'{platform_name}_cookies.json',
    ]
    
    for cookie_file in possible_paths:
        if not cookie_file.exists():
            continue
            
        try:
            with open(cookie_file, 'r', encoding='utf-8') as f:
                cookie_data = json.load(f)

            cookies = {}
            if isinstance(cookie_data, list):
                for cookie in cookie_data:
                    name, value = cookie.get('name'), cookie.get('value')
                    if name and value: 
                        cookies[name] = value
            elif isinstance(cookie_data, dict):
                cookies = cookie_data
            
            if cookies:
                return cookies
        except Exception:
            continue

    return {}

def _get_bilibili_credential() -> Any | None:
    try:
        from bilibili_api import Credential
        cookies = _get_platform_cookies('bilibili')
        if not cookies:
            return None
        
        # 兼容原版逻辑，加入 buvid3 防止被 B站 API 风控拦截
        return Credential(
            sessdata=cookies.get('SESSDATA', ''),
            bili_jct=cookies.get('bili_jct', ''),
            buvid3=cookies.get('buvid3', ''),
            dedeuserid=cookies.get('DedeUserID', '')
        )
    except ImportError:
        logger.debug("bilibili_api 库未安装")
        return None
    except Exception as e:
        logger.debug(f"从文件加载认证信息失败: {e}")
    
    return None


# ==========================================
# 独立业务层 A：公共与首页推荐 (Trending & public - 公开接口，无需认证)
# ==========================================

async def fetch_bilibili_trending(limit: int = 30) -> dict[str, Any]:
    try:
        from bilibili_api import homepage
        credential = _get_bilibili_credential()
        await asyncio.sleep(random.uniform(0.1, 0.5))
        result = await homepage.get_videos(credential=credential)
        
        videos = []
        if result:
            data = result.get('data', result)
            items = data.get('item', [])
            
            for item in items:
                bvid = item.get('bvid', '')
                if not bvid:
                    continue
                
                rcmd_reason = item.get('rcmd_reason', {})
                rcmd_reason_text = rcmd_reason.get('content', '') if isinstance(rcmd_reason, dict) else str(rcmd_reason)
                    
                videos.append({
                    'title': item.get('title', ''),
                    'desc': item.get('desc', ''),
                    'author': item.get('owner', {}).get('name', ''),
                    'view': item.get('stat', {}).get('view', 0),
                    'like': item.get('stat', {}).get('like', 0),
                    'bvid': bvid,
                    'url': f'https://www.bilibili.com/video/{bvid}',
                    'id': item.get('id', 0),
                    'goto': item.get('goto', ''),
                    'rcmd_reason': rcmd_reason_text,
                })
                if len(videos) >= limit: break
        
        logger.info(f"✅ 成功获取 {len(videos)} 个B站推荐视频")
        return {'success': True, 'videos': videos}
    except ImportError:
        logger.error("bilibili_api 库未安装，请运行: pip install bilibili-api-python")
        return {'success': False, 'error': 'bilibili_api 库未安装'}
    except Exception as e:
        logger.error(f"获取B站首页推荐视频失败: {e}")
        import traceback
        logger.debug(f"错误详情:\n{traceback.format_exc()}")
        return {'success': False, 'error': str(e)}

async def fetch_douyin_trending(limit: int = 10) -> Dict[str, Any]:
    """
    获取抖音公共热搜榜
    """
    pass

async def fetch_kuaishou_trending(limit: int = 10) -> Dict[str, Any]:
    """获取快手公共热搜榜 (GraphQL 接口)"""
    pass

async def fetch_weibo_trending(limit: int = 10) -> dict[str, Any]:
    try:
        url = "https://s.weibo.com/top/summary?cate=realtimehot"
        headers = {
            'User-Agent': get_random_user_agent(), 'Referer': 'https://s.weibo.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        }
        
        weibo_cookies = _get_platform_cookies('weibo')
        if weibo_cookies:
            headers['Cookie'] = '; '.join([f"{k}={v}" for k, v in weibo_cookies.items()])
        else:
            if env_cookie := os.getenv("WEIBO_FALLBACK_COOKIE"):
                headers['Cookie'] = env_cookie

        await asyncio.sleep(random.uniform(0.1, 0.5))
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if 'passport' in str(response.url): 
                logger.warning("微博未提供有效Cookie或已过期，回退到公开API")
                return await _fetch_weibo_trending_fallback(limit)
            
            soup = BeautifulSoup(response.text, 'html.parser')
            td_items = soup.find_all('td', class_='td-02')
            if not td_items: 
                logger.warning("s.weibo.com未返回有效热搜数据，回退到公开API")
                return await _fetch_weibo_trending_fallback(limit)
            
            trending_list = []
            for i, td in enumerate(td_items):
                if len(trending_list) >= limit: break
                a_tag = td.find('a')
                span = td.find('span')
                if a_tag and (word := a_tag.get_text(strip=True)):
                    if not word:
                        continue
                    href = a_tag.get('href', '')
                    if href and not href.startswith('http'):
                        href = f"https://s.weibo.com{href}"
                    
                    hot_text = span.get_text(strip=True) if span else ''
                    hot_match = re.search(r'(\d+)', hot_text)
                    raw_hot = int(hot_match.group(1)) if hot_match else 0
                    note = re.sub(r'\d+', '', hot_text).strip() if hot_text else ''
                    
                    trending_list.append({
                        'word': word, 'raw_hot': raw_hot, 'note': note,
                        'rank': i + 1, 'url': href
                    })
            
            if trending_list:
                logger.info(f"成功从s.weibo.com获取{len(trending_list)}条热搜")
                return {'success': True, 'trending': trending_list}
            else:
                return await _fetch_weibo_trending_fallback(limit)
                
    except Exception as e:
        logger.warning(f"s.weibo.com热搜获取失败: {e}，回退到公开API")
        return await _fetch_weibo_trending_fallback(limit)


async def _fetch_weibo_trending_fallback(limit: int = 10) -> Dict[str, Any]:
    try:
        url = "https://weibo.com/ajax/side/hotSearch"
        headers = {
            'User-Agent': get_random_user_agent(),
            'Referer': 'https://weibo.com',
            'Accept': 'application/json, text/plain, */*',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'DNT': '1',
            'Cache-Control': 'no-cache',
            'Pragma': 'no-cache',
        }
        
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            # 第三方 API 的防御性 JSON 校验
            if not isinstance(data, dict): 
                return {'success': False, 'error': 'API返回格式异常'}

            if data.get('ok') == 1:
                trending_list = [
                    {
                        'word': (word := item.get('word', '')), 'raw_hot': item.get('raw_hot', 0),
                        'note': item.get('note', ''), 'rank': item.get('rank', 0),
                        'url': f"https://s.weibo.com/weibo?q={quote(word)}" if word else ''
                    }
                    for item in data.get('data', {}).get('realtime', [])[:limit] if not item.get('is_ad')
                ]
                return {'success': True, 'trending': trending_list}
            return {'success': False, 'error': '微博API返回错误'}
    except Exception as e: return {'success': False, 'error': str(e)}

def _format_score(count: int) -> str:
    """
    格式化Reddit的分数，超过1000显示K，超过100万显示M
    """
    if count >= 1_000_000: return f"{count / 1_000_000:.1f}M"
    elif count >= 1_000: return f"{count / 1_000:.1f}K"
    elif count > 0: return str(count)
    return "0"

async def fetch_reddit_popular(limit: int = 10) -> dict[str, Any]:
    try:
        url = f"https://www.reddit.com/r/popular/hot.json?limit={limit}"
        headers = {'User-Agent': get_random_user_agent(), 'Accept': 'application/json'}
        await asyncio.sleep(random.uniform(0.1, 0.5))
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            data = response.json()
            posts = []
            for item in data.get('data', {}).get('children', [])[:limit]:
                pd = item.get('data', {})
                if pd.get('over_18'): continue
                posts.append({
                    'title': pd.get('title', ''), 'subreddit': f"r/{pd.get('subreddit', '')}",
                    'score': _format_score(pd.get('score', 0)), 'url': f"https://www.reddit.com{pd.get('permalink', '')}"
                })
            if posts:
                logger.info(f"从Reddit获取到{len(posts)}条热门帖子")
                return {'success': True, 'posts': posts}
            return {'success': False, 'error': 'Reddit返回空数据'}
    except Exception as e: return {'success': False, 'error': str(e)}

async def fetch_twitter_trending(limit: int = 10) -> dict[str, Any]:
    try:
        url = "https://trends24.in/"
        headers = {'User-Agent': get_random_user_agent()}
        await asyncio.sleep(random.uniform(0.1, 0.3))
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            soup = BeautifulSoup(response.text, 'html.parser')
            trending_list = [
                {'word': trend_text, 'url': f"https://twitter.com/search?q={quote(trend_text)}"}
                for item in soup.select('.trend-card__list li a')[:limit]
                if (trend_text := item.get_text(strip=True))
            ]
            if trending_list: return {'success': True, 'trending': trending_list}
    except Exception: pass
    return {'success': False, 'error': '无法获取Twitter热门'}


# ==========================================
# 独立业务层 B：个人推荐/流 (Personal Dynamics - 需要用户认证信息)
# ==========================================

async def fetch_bilibili_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """
    获取B站推送的动态消息
    设计原则：
    - 仅需核心登录凭证 SESSDATA，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    import re

    try:
        credential = _get_bilibili_credential()
        if not credential: 
            return {'success': False, 'error': '未提供Bilibili认证信息'}

        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
        headers = {"User-Agent": get_random_user_agent(), "Referer": "https://t.bilibili.com/"}
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, cookies=credential.get_cookies(), timeout=10.0)
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict) or data.get("code") != 0:
            logger.error(f"获取B站动态失败，API返回: {data}")
            return {'success': False, 'error': f"API请求失败"}

        def safe_dict(d: Any, key: str) -> dict:
            if not isinstance(d, dict): return {}
            v = d.get(key)
            return v if isinstance(v, dict) else {}

        dynamic_list = []
        items = data.get("data")
        items = items.get("items", []) if isinstance(items, dict) else []

        for item in items:
            if not isinstance(item, dict): continue
                
            try:
                dynamic_id = str(item.get("id_str", ""))
                dynamic_type = str(item.get("type", ""))
                if dynamic_type in {"DYNAMIC_TYPE_AD", "DYNAMIC_TYPE_APPLET", "DYNAMIC_TYPE_NONE"}: 
                    continue
                
                modules = safe_dict(item, "modules")
                module_author = safe_dict(modules, "module_author")
                
                # 获取到了作者名
                author = module_author.get("name") or "未知UP主"
                pub_time = module_author.get("pub_time") or "刚刚"
                
                module_dynamic = safe_dict(modules, "module_dynamic")
                major = safe_dict(module_dynamic, "major")
                desc = safe_dict(module_dynamic, "desc")
                
                major_type = major.get("type")
                raw_text = desc.get("text") or ""
                
                content = ""
                specific_url = f"https://t.bilibili.com/{dynamic_id}"  # 默认动态页面URL
                
                match major_type:
                    case "MAJOR_TYPE_ARCHIVE": 
                        # 视频动态：添加视频链接
                        archive = safe_dict(major, "archive")
                        bvid = archive.get("bvid", "")
                        if bvid:
                            specific_url = f"https://www.bilibili.com/video/{bvid}"
                        content = f"[发布了新视频] {archive.get('title', '')}"
                        
                    case "MAJOR_TYPE_DRAW": 
                        # 图文动态：保持动态页面链接
                        content = f"[图文动态] {raw_text}" if raw_text else "[分享了图片]"
                        
                    case "MAJOR_TYPE_ARTICLE":
                        # 专栏文章：添加文章链接
                        article = safe_dict(major, "article")
                        article_id = article.get("id", "")
                        if article_id:
                            specific_url = f"https://www.bilibili.com/read/cv{article_id}"
                        content = f"[发布了专栏文章] {article.get('title', '')}"
                        
                    case "MAJOR_TYPE_LIVE_RCMD":
                        # 直播动态：添加直播间链接
                        live_title = raw_text
                        try:
                            live_rcmd = major.get("live_rcmd") or major.get("live")
                            if isinstance(live_rcmd, dict):
                                content_str = live_rcmd.get("content")
                                if isinstance(content_str, str) and content_str.startswith("{"):
                                    play_info = json.loads(content_str).get("live_play_info")
                                    if isinstance(play_info, dict):
                                        live_title = play_info.get("title", live_title)
                                        room_id = play_info.get("room_id")
                                        if room_id:
                                            specific_url = f"https://live.bilibili.com/{room_id}"
                                elif isinstance(live_rcmd.get("live_play_info"), dict):
                                    live_title = live_rcmd["live_play_info"].get("title", live_title)
                                    room_id = live_rcmd["live_play_info"].get("room_id")
                                    if room_id:
                                        specific_url = f"https://live.bilibili.com/{room_id}"
                        except Exception: pass
                        content = f"[正在直播] {live_title or '快来我的直播间看看吧！'}"
                        
                    case _:
                        if dynamic_type == "DYNAMIC_TYPE_LIVE_RCMD":
                            # 直播开播推送：添加直播间链接
                            content = f"[正在直播] {raw_text or '快来我的直播间看看吧！'}"
                            # 尝试从描述中提取直播间ID
                            import re
                            room_match = re.search(r'直播间：(\d+)', raw_text)
                            if room_match:
                                specific_url = f"https://live.bilibili.com/{room_match.group(1)}"
                                
                        elif dynamic_type == "DYNAMIC_TYPE_FORWARD":
                            content = f"[转发动态] {raw_text}" if raw_text else "[转发了动态]"
                        else:
                            content = raw_text or "发布了新动态"

                content = re.sub(r'\s+', ' ', content).strip()
                if not content: content = "分享了新动态"

                final_content = f"UP主【{author}】: {content}"

                dynamic_list.append({
                    'dynamic_id': dynamic_id, 'type': dynamic_type, 'timestamp': pub_time,
                    'author': author, 'content': final_content,  # 存入拼接好的完整字符串
                    'url': specific_url,  # 使用具体类型的URL
                    'base_url': f"https://t.bilibili.com/{dynamic_id}"  # 保留原始动态页面链接
                })
                if len(dynamic_list) >= limit: break
            except Exception as item_e:
                logger.warning(f"解析单条动态失败，跳过: {item_e}, 动态ID: {item.get('id_str', '未知')}")

        if dynamic_list:
            logger.info(f"✅ 成功获取到 {len(dynamic_list)} 条你关注的UP主动态消息")
        return {'success': True, 'dynamics': dynamic_list}

    except Exception as e:
        logger.error(f"获取B站动态消息失败: {e}")
        return {'success': False, 'error': str(e)}
        
async def fetch_douyin_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    pass

async def fetch_kuaishou_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """获取快手个人关注动态 (GraphQL 接口 + 严格 Cookie)"""
    pass

async def fetch_weibo_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """
    获取微博动态
    设计原则：
    - 切换至 Mobile 移动版 API，彻底绕过 PC 端所有风控
    - 仅需核心登录凭证 SUB，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    import re
    import random
    import asyncio
    import httpx
    
    try:
        weibo_cookies = _get_platform_cookies('weibo')
        if not weibo_cookies:
            return {'success': False, 'error': '未找到 config/weibo_cookies.json'}
        
        # 1. 只需要最核心的 SUB，其他全都不需要！
        sub = weibo_cookies.get('SUB') or weibo_cookies.get('sub')
        if not sub:
            logger.error("❌ 缺少核心登录凭证 SUB。")
            return {'success': False, 'error': '缺少核心登录凭证 SUB'}

        # 2. 目标变更为：移动端首页关注流的固定 Container ID
        url = "https://m.weibo.cn/api/container/getIndex?containerid=102803"
        
        # 3. 必须伪装成手机浏览器的 User-Agent
        mobile_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        
        headers = {
            'User-Agent': mobile_ua,
            'Referer': 'https://m.weibo.cn/',
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
            'MWeibo-Pwa': '1'
        }
        
        # 仅携带最纯净的 SUB 即可
        req_cookies = {'SUB': sub}
        
        await asyncio.sleep(random.uniform(0.1, 0.5))

        # 4. 移动端 API 非常宽容，直接用普通的 httpx 即可稳定发包
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, cookies=req_cookies)
            
            if response.status_code != 200:
                logger.error(f"❌ 移动端微博接口异常，状态码: {response.status_code}")
                return {'success': False, 'error': f"API请求失败，状态码: {response.status_code}"}
                
            data = response.json()
            
            # 移动端如果未登录，通常会返回 ok: 0 或者重定向
            if data.get('ok') != 1:
                logger.error("❌ 微博拦截：返回 ok=0，说明你的 SUB 凭证已过期！")
                return {'success': False, 'error': "微博凭证已过期，请去浏览器重新获取"}
            
            cards = data.get('data', {}).get('cards', [])
            weibo_list = []
            
            for card in cards:
                # card_type == 9 代表这是一条正常的微博博文卡片
                if card.get('card_type') != 9:
                    continue
                    
                mblog = card.get('mblog')
                if not mblog:
                    continue
                    
                user = mblog.get('user', {})
                author = user.get('screen_name') or '未知博主'
                
                # 提取正文并清理 HTML 标签
                text = str(mblog.get('text') or '')
                clean_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()
                
                # 兼容并缝合转发内容
                if mblog.get('retweeted_status'):
                    retweet = mblog['retweeted_status']
                    rt_author = retweet.get('user', {}).get('screen_name') or '原博主'
                    rt_text = str(retweet.get('text') or '')
                    rt_clean_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', rt_text)).strip()
                    clean_text = f"{clean_text} // [转发动态] @{rt_author}: {rt_clean_text}"
                
                display_text = clean_text if clean_text else "[分享了图片/动态]"
                final_content = f"博主【{author}】: {display_text}"
                mid = mblog.get('mid') or mblog.get('id', '')
                
                weibo_list.append({
                    'author': author,
                    'content': final_content,
                    'timestamp': mblog.get('created_at') or '',
                    'url': f"https://m.weibo.cn/detail/{mid}" # 使用移动端 URL
                })
                
                if len(weibo_list) >= limit:
                    break

            if weibo_list: 
                logger.info(f"✅ 成功通过移动端接口获取到 {len(weibo_list)} 条微博个人动态")
                logger.info("微博动态:")  # 统一对齐 B站 的提示词
                for i, weibo in enumerate(weibo_list, 1):
                    content = weibo.get('content', '')
                    # 稍微放宽一点截断长度，保证显示效果更好
                    if len(content) > 50:
                        content = content[:50] + "..."
                    # 去掉冗余的时间和作者，直接干干净净地打印 content
                    logger.info(f"  - {content}")
                
                return {'success': True, 'statuses': weibo_list}
            else:
                return {'success': False, 'error': '未解析到微博内容'}
                
    except Exception as e: 
        logger.error(f"微博动态解析发生错误: {e}")
        return {'success': False, 'error': str(e)}

async def fetch_reddit_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """
    获取Reddit推送的动态帖子
    设计原则：
    - 仅需核心登录凭证 reddit_session，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    try:
        reddit_cookies = _get_platform_cookies('reddit')
        if not reddit_cookies: 
            return {'success': False, 'error': '未配置 config/reddit_cookies.json'}
        url = f"https://www.reddit.com/hot.json?limit={limit}"
        headers = {'User-Agent': get_random_user_agent(), 'Accept': 'application/json'}
        await asyncio.sleep(random.uniform(0.1, 0.5))

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, cookies=reddit_cookies)
            data = response.json()
            posts = [
                {
                    'title': pd.get('title', ''), 'subreddit': f"r/{pd.get('subreddit', '')}",
                    'score': _format_score(pd.get('score', 0)), 
                    'url': f"https://www.reddit.com{pd.get('permalink', '')}"
                }
                for item in data.get('data', {}).get('children', [])[:limit]
                if not (pd := item.get('data', {})).get('over_18')
            ]
            if posts: logger.info(f"✅ 成功获取到 {len(posts)} 条Reddit订阅帖子")
            return {'success': True, 'posts': posts}
    except Exception as e: 
        return {'success': False, 'error': str(e)}


async def _fetch_twitter_personal_web_scraping(limit: int = 10, cookies: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Twitter 网页抓取 fallback
    设计原则：
    - 仅需核心登录凭证 twitter_session，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    try:
        url = "https://twitter.com/home"
        headers = {'User-Agent': get_random_user_agent()}
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            res = await client.get(url, headers=headers, cookies=cookies)
            
            # 如果被重定向到了登录页，说明 Cookie 彻底失效了
            if "login" in str(res.url) or "logout" in str(res.url):
                return {'success': False, 'error': 'Twitter Cookie 已过期，网页端拒绝访问'}
                
            tweets = []
            tweet_texts = re.findall(r'"tweet":\{[^}]*"full_text":"([^"]+)"', res.text)
            screen_names = re.findall(r'"screen_name":"([^"]+)"', res.text)
            
            for i, text in enumerate(tweet_texts[:limit]):
                clean_text = re.sub(r'https://t\.co/\w+', '', text).strip()
                tweets.append({
                    'author': f"@{screen_names[i] if i<len(screen_names) else 'Unknown'}", 
                    'content': clean_text,
                    'timestamp': '刚刚'  # 保持与主 API 数据字典格式的统一
                })
                
            return {'success': True, 'tweets': tweets} if tweets else {'success': False, 'error': '网页正则抓取失败，页面结构可能已变更'}
    except Exception as e: 
        logger.error(f"Twitter 网页抓取 fallback 失败: {e}")
        return {'success': False, 'error': str(e)}

async def fetch_twitter_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """
    获取 Twitter 个人时间线
    设计原则：
    - 仅需核心登录凭证 twitter_session，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    
    try:
        twitter_cookies = _get_platform_cookies('twitter')
        if not twitter_cookies:
             return {'success': False, 'error': '未配置 config/twitter_cookies.json'}
             
        # 提取防伪 CSRF Token。Twitter 必须，否则哪怕有合法 Cookie 也会立刻 401/403
        ct0 = twitter_cookies.get('ct0') or twitter_cookies.get('CT0', '')
        if not ct0:
            logger.warning("Twitter Cookie 中缺少核心字段 ct0，极大可能触发风控拦截")
        
        # 官方 Web 客户端通用固化的 Bearer Token
        bearer_token = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIyU2%2FGoa3FmBNYDPz%2FzGz%2F2Rnc%2F2bGBDH%2Fc'
        
        # 切换到更稳定、包含完整推文文本的 v1.1 接口
        url = f"https://api.twitter.com/1.1/statuses/home_timeline.json?tweet_mode=extended&count={limit}"
        
        # 补全极其严格的 Twitter 风控协议头
        headers = {
            'User-Agent': get_random_user_agent(), 
            'Accept': 'application/json',
            'Authorization': f'Bearer {bearer_token}',
            'x-twitter-auth-type': 'OAuth2Session' if 'auth_token' in twitter_cookies else '',
            'x-csrf-token': ct0,  # <-- 防火墙放行的关键钥匙
            'x-twitter-active-user': 'yes',
            'x-twitter-client-language': 'zh-cn'
        }
        
        await asyncio.sleep(random.uniform(0.1, 0.5))

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, cookies=twitter_cookies)
            
            # 状态码非 200 时，平滑降级到备用网页刮削方案
            if response.status_code != 200: 
                logger.warning(f"Twitter API 拒绝访问 (状态码: {response.status_code})，回退到网页刮削...")
                return await _fetch_twitter_personal_web_scraping(limit, twitter_cookies)
                
            # 真正去解析返回的推文数据，替换掉之前的占位符
            data = response.json()
            if not isinstance(data, list):
                return {'success': False, 'error': 'API 返回数据格式异常'}
                
            tweets = []
            for tweet in data[:limit]:
                user = tweet.get('user', {})
                author = user.get('screen_name') or 'Unknown'
                # tweet_mode=extended 时，正文在 full_text 里
                text = str(tweet.get('full_text') or tweet.get('text') or '')
                
                # 清理推文末尾自带的分享短链接 (https://t.co/xxx)
                clean_text = re.sub(r'https://t\.co/\w+', '', text).strip()
                
                # 处理转推 (Retweet) 的前缀拼接
                if 'retweeted_status' in tweet:
                    rt_user = tweet['retweeted_status'].get('user', {}).get('screen_name', 'Unknown')
                    rt_text = str(tweet['retweeted_status'].get('full_text') or '')
                    rt_clean_text = re.sub(r'https://t\.co/\w+', '', rt_text).strip()
                    clean_text = f"RT @{rt_user}: {rt_clean_text}"
                
                tweets.append({
                    'author': f"@{author}", 
                    'content': clean_text,
                    'timestamp': tweet.get('created_at', '')
                })
                
            if tweets:
                logger.info(f"✅ 成功获取到 {len(tweets)} 条 Twitter 个人时间线动态")
                return {'success': True, 'tweets': tweets}
            else:
                return {'success': False, 'error': '未解析到推文内容'}
                
    except Exception as e: 
        logger.error(f"Twitter API 获取失败: {e}")
        return {'success': False, 'error': str(e)}

# ==========================================
# 核心调度与格式化层 (Public & Personal)
# ==========================================

async def fetch_public_content(bilibili_limit: int = 10, douyin_limit: int = 10, kuaishou_limit: int = 10, 
                                weibo_limit: int = 10,
                                reddit_limit: int = 10, twitter_limit: int = 10) -> Dict[str, Any]:
    """
    获取公共热门内容
    """
    try:
        china_region = is_china_region()
        if china_region:
            logger.info("检测到中文区域，获取B站和微博热门内容")
            bilibili_task = fetch_bilibili_trending(bilibili_limit)
            weibo_task = fetch_weibo_trending(weibo_limit)

            bilibili_result,  weibo_result = await asyncio.gather(
                bilibili_task, weibo_task,  return_exceptions=True
            )

            if isinstance(bilibili_result, Exception):
                logger.error(f"B站爬取异常: {bilibili_result}")
                bilibili_result = {'success': False, 'error': str(bilibili_result)}

            if isinstance(weibo_result, Exception):
                logger.error(f"微博爬取异常: {weibo_result}")
                weibo_result = {'success': False, 'error': str(weibo_result)}
            
            if not bilibili_result.get('success') and not weibo_result.get('success') :
                return {'success': False, 'error': '无法获取任何热门内容', 'region': 'china', 'bilibili': bilibili_result, 'weibo': weibo_result}
            
            return {'success': True, 'region': 'china', 'bilibili': bilibili_result, 'weibo': weibo_result}
        else:
            logger.info("检测到非中文区域，获取Reddit和Twitter热门内容")
            reddit_task = fetch_reddit_popular(reddit_limit)
            twitter_task = fetch_twitter_trending(twitter_limit)
            
            reddit_result, twitter_result = await asyncio.gather(
                reddit_task, twitter_task, return_exceptions=True
            )
            
            if isinstance(reddit_result, Exception):
                logger.error(f"Reddit爬取异常: {reddit_result}")
                reddit_result = {'success': False, 'error': str(reddit_result)}
            
            if isinstance(twitter_result, Exception):
                logger.error(f"Twitter爬取异常: {twitter_result}")
                twitter_result = {'success': False, 'error': str(twitter_result)}
            
            if not reddit_result.get('success') and not twitter_result.get('success'):
                return {'success': False, 'error': '无法获取任何热门内容', 'region': 'non-china', 'reddit': reddit_result, 'twitter': twitter_result}
            
            return {'success': True, 'region': 'non-china', 'reddit': reddit_result, 'twitter': twitter_result}
    except Exception as e:
        logger.error(f"获取热门内容失败: {e}")
        return {
            'success': False,
            'error': str(e)
        }


async def _fetch_content_by_region(
    china_fetch_func,
    non_china_fetch_func,
    limit: int,
    content_key: str,
    china_log_msg: str,
    non_china_log_msg: str
) -> Dict[str, Any]:
    """
    根据用户区域获取内容的通用辅助函数
    
    Args:
        china_fetch_func: 中文区域使用的异步获取函数
        non_china_fetch_func: 非中文区域使用的异步获取函数
        limit: 内容最大数量
        content_key: 返回结果中的内容键名 ('video' 或 'news')
        china_log_msg: 中文区域的日志消息
        non_china_log_msg: 非中文区域的日志消息
    
    Returns:
        包含成功状态和内容的字典
    """
    china_region = is_china_region()
    region = 'china' if china_region else 'non-china'
    
    try:
        if china_region:
            logger.info(china_log_msg)
            result = await china_fetch_func(limit)
            response = {
                'success': result.get('success', False),
                'region': region,
                content_key: result
            }
        else:
            logger.info(non_china_log_msg)
            result = await non_china_fetch_func(limit)
            response = {
                'success': result.get('success', False),
                'region': region,
                content_key: result
            }
        
        if not result.get('success') and result.get('error'):
            response['error'] = result.get('error')
        return response
            
    except Exception as e:
        logger.error(f"获取内容失败: content_key={content_key} region={region} error={e}")
        return {
            'success': False,
            'error': str(e)
        }


async def fetch_video_content(limit: int = 10) -> Dict[str, Any]:
    """
    根据用户区域获取视频内容
    
    中文区域：获取B站首页视频
    非中文区域：获取Reddit热门帖子
    
    Args:
        limit: 内容最大数量
    
    Returns:
        包含成功状态和视频内容的字典
    """
    return await _fetch_content_by_region(
        china_fetch_func=fetch_bilibili_trending,
        non_china_fetch_func=fetch_reddit_popular,
        limit=limit,
        content_key='video',
        china_log_msg="检测到中文区域，获取B站视频内容",
        non_china_log_msg="检测到非中文区域，获取Reddit热门内容"
    )


async def fetch_news_content(limit: int = 10) -> Dict[str, Any]:
    """
    根据用户区域获取新闻/热议话题内容
    
    中文区域：获取微博热议话题
    非中文区域：获取Twitter热门话题
    
    Args:
        limit: 内容最大数量
    
    Returns:
        包含成功状态和新闻内容的字典
    """
    return await _fetch_content_by_region(
        china_fetch_func=fetch_weibo_trending,
        non_china_fetch_func=fetch_twitter_trending,
        limit=limit,
        content_key='news',
        china_log_msg="检测到中文区域，获取微博热议话题",
        non_china_log_msg="检测到非中文区域，获取Twitter热门话题"
    )


def _format_bilibili_videos(videos: List[Dict], limit: int = 5) -> List[str]:
    """格式化B站视频列表"""
    output_lines = ["【B站首页推荐】"]
    for i, video in enumerate(videos[:limit], 1):
        title = video.get('title', '')
        author = video.get('author', '')
        rcmd_reason = video.get('rcmd_reason', '')
        
        output_lines.append(f"{i}. {title}")
        output_lines.append(f"   UP主: {author}")
        if rcmd_reason:
            output_lines.append(f"   推荐理由: {rcmd_reason}")
    output_lines.append("")
    return output_lines


def _format_reddit_posts(posts: List[Dict], limit: int = 5) -> List[str]:
    """格式化Reddit帖子列表"""
    output_lines = ["【Reddit Hot Posts】"]
    for i, post in enumerate(posts[:limit], 1):
        title = post.get('title', '')
        subreddit = post.get('subreddit', '')
        score = post.get('score', '')
        
        output_lines.append(f"{i}. {title}")
        if subreddit:
            output_lines.append(f"   {subreddit} | {score} upvotes")
    output_lines.append("")
    return output_lines


def _format_weibo_trending(trending_list: List[Dict], limit: int = 5) -> List[str]:
    """格式化微博热议话题列表"""
    output_lines = ["【微博热议话题】"]
    for i, item in enumerate(trending_list[:limit], 1):
        word = item.get('word', '')
        note = item.get('note', '')
        
        line = f"{i}. {word}"
        if note:
            line += f" [{note}]"
        output_lines.append(line)
    output_lines.append("")
    return output_lines


def _format_twitter_trending(trending_list: List[Dict], limit: int = 5) -> List[str]:
    """格式化Twitter热门话题列表"""
    output_lines = ["【Twitter Trending Topics】"]
    for i, item in enumerate(trending_list[:limit], 1):
        word = item.get('word', '')
        tweet_count = item.get('tweet_count', '')
        
        line = f"{i}. {word}"
        if tweet_count and tweet_count != 'N/A':
            line += f" ({tweet_count} tweets)"
        output_lines.append(line)
    output_lines.append("")
    return output_lines


def format_trending_content(trending_content: Dict[str, Any]) -> str:
    """
    将热门内容格式化为可读字符串
    
    根据区域自动格式化：
    - 中文区域：B站和微博内容，中文显示
    - 非中文区域：Reddit和Twitter内容，英文显示
    
    Args:
        trending_content: fetch_trending_content返回的结果
    
    Returns:
        格式化后的字符串
    """
    output_lines = []
    region = trending_content.get('region', 'china')
    
    if region == 'china':
        bilibili_data = trending_content.get('bilibili', {})
        if bilibili_data.get('success'):
            videos = bilibili_data.get('videos', [])
            output_lines.extend(_format_bilibili_videos(videos))
        
        weibo_data = trending_content.get('weibo', {})
        if weibo_data.get('success'):
            trending_list = weibo_data.get('trending', [])
            output_lines.extend(_format_weibo_trending(trending_list))
        
        if not output_lines:
            return "暂时无法获取推荐内容"
            
    else:
        reddit_data = trending_content.get('reddit', {})
        if reddit_data.get('success'):
            posts = reddit_data.get('posts', [])
            output_lines.extend(_format_reddit_posts(posts))
        
        twitter_data = trending_content.get('twitter', {})
        if twitter_data.get('success'):
            trending_list = twitter_data.get('trending', [])
            output_lines.extend(_format_twitter_trending(trending_list))
        
        if not output_lines:
            return "Unable to fetch trending content at the moment"
    
    return "\n".join(output_lines)


def format_video_content(video_content: Dict[str, Any]) -> str:
    """
    将视频内容格式化为可读字符串
    
    根据区域自动格式化：
    - 中文区域：B站视频内容
    - 非中文区域：Reddit帖子内容
    
    Args:
        video_content: fetch_video_content返回的结果
    
    Returns:
        格式化后的字符串
    """
    region = video_content.get('region', 'china')
    video_data = video_content.get('video', {})
    
    if region == 'china':
        if video_data.get('success'):
            videos = video_data.get('videos', [])
            output_lines = _format_bilibili_videos(videos)
            return "\n".join(output_lines)
        return "暂时无法获取视频推荐内容"
    else:
        if video_data.get('success'):
            posts = video_data.get('posts', [])
            output_lines = _format_reddit_posts(posts)
            return "\n".join(output_lines)
        return "Unable to fetch trending posts at the moment"


def format_news_content(news_content: Dict[str, Any]) -> str:
    """
    将新闻内容格式化为可读字符串
    
    根据区域自动格式化：
    - 中文区域：微博热议话题
    - 非中文区域：Twitter热门话题
    
    Args:
        news_content: fetch_news_content返回的结果
    
    Returns:
        格式化后的字符串
    """
    region = news_content.get('region', 'china')
    news_data = news_content.get('news', {})
    
    if region == 'china':
        if news_data.get('success'):
            trending_list = news_data.get('trending', [])
            output_lines = _format_weibo_trending(trending_list)
            return "\n".join(output_lines)
        return "暂时无法获取热议话题"
    else:
        if news_data.get('success'):
            trending_list = news_data.get('trending', [])
            output_lines = _format_twitter_trending(trending_list)
            return "\n".join(output_lines)
        return "Unable to fetch trending topics at the moment"


def get_active_window_title(include_raw: bool = False) -> Optional[Union[str, Dict[str, str]]]:
    """
    获取当前活跃窗口的标题（仅支持Windows）
    
    Args:
        include_raw: 是否返回原始标题。默认False，仅返回截断后的安全标题。
                     设为True时返回包含sanitized和raw的字典。
    
    Returns:
        默认情况：截断后的安全标题字符串（前30字符），失败返回None
        include_raw=True时：{'sanitized': '截断标题', 'raw': '完整标题'}，失败返回None
    """
    if platform.system() != 'Windows':
        logger.warning("获取活跃窗口标题仅支持Windows系统")
        return None
    try:
        import pygetwindow as gw
    except ImportError:
        logger.error("pygetwindow模块未安装。在Windows系统上请安装: pip install pygetwindow")
        return None
    try:
        active_window = gw.getActiveWindow()
        if active_window:
            raw_title = active_window.title
            sanitized_title = raw_title[:30] + '...' if len(raw_title) > 30 else raw_title
            logger.info(f"获取到活跃窗口标题: {sanitized_title}")
            
            if include_raw:
                return {'sanitized': sanitized_title, 'raw': raw_title}
            else:
                return sanitized_title
        else:
            logger.warning("没有找到活跃窗口")
            return None
    except Exception as e:
        logger.exception(f"获取活跃窗口标题失败: {e}")
        return None

async def generate_diverse_queries(window_title: str) -> List[str]:
    try:
        from utils.config_manager import ConfigManager
        config_manager = ConfigManager()
        correction_config = config_manager.get_model_api_config('correction')
        
        llm = ChatOpenAI(
            model=correction_config['model'],
            base_url=correction_config['base_url'],
            api_key=correction_config['api_key'],
            temperature=1.0, timeout=10.0
        )
        
        sanitized_title = window_title[:30] + '...' if len(window_title) > 30 else window_title
        china_region = is_china_region()
        
        if china_region:
            prompt = f"""基于以下窗口标题，生成3个不同的搜索关键词，用于在百度上搜索相关内容。\n\n窗口标题：{window_title}\n\n要求：\n1. 生成3个不同角度的搜索关键词\n2. 关键词应该简洁（2-8个字）\n3. 关键词应该多样化，涵盖不同方面\n4. 只输出3个关键词，每行一个，不要添加任何序号、标点或其他内容\n\n示例输出格式：\n关键词1\n关键词2\n关键词3"""
        else:
            prompt = f"""Based on the following window title, generate 3 different search keywords for Google search.\n\nWindow title: {window_title}\n\nRequirements:\n1. Generate 3 keywords from different angles\n2. Keywords should be concise (2-6 words each)\n3. Keywords should be diverse, covering different aspects\n4. Output only 3 keywords, one per line, without any numbers, punctuation, or other content\n\nExample output format:\nkeyword one\nkeyword two\nkeyword three"""

        response = await llm.ainvoke([SystemMessage(content=prompt)])
        queries = []
        lines = response.content.strip().split('\n')
        for line in lines:
            line = line.strip()
            line = re.sub(r'^[\d\.\-\*\)\]】]+\s*', '', line)
            line = line.strip('.,;:，。；：')
            if line and len(line) >= 2:
                queries.append(line)
                if len(queries) >= 3: break
        
        if len(queries) < 3:
            clean_title = clean_window_title(window_title)
            while len(queries) < 3 and clean_title:
                queries.append(clean_title)
        
        logger.info(f"为窗口标题「{sanitized_title}」生成的查询关键词: {queries}")
        return queries[:3]
        
    except Exception as e:
        sanitized_title = window_title[:30] + '...' if len(window_title) > 30 else window_title
        logger.warning(f"为窗口标题「{sanitized_title}」生成多样化查询失败，使用默认清理方法: {e}")
        clean_title = clean_window_title(window_title)
        return [clean_title, clean_title, clean_title]

def clean_window_title(title: str) -> str:
    if not title: return ""
    patterns_to_remove = [
        r'\s*[-–—]\s*(Google Chrome|Mozilla Firefox|Microsoft Edge|Opera|Safari|Brave).*$',
        r'\s*[-–—]\s*(Visual Studio Code|VS Code|VSCode).*$',
        r'\s*[-–—]\s*(记事本|Notepad\+*|Sublime Text|Atom).*$',
        r'\s*[-–—]\s*(Microsoft Word|Excel|PowerPoint).*$',
        r'\s*[-–—]\s*(QQ音乐|网易云音乐|酷狗音乐|Spotify).*$',
        r'\s*[-–—]\s*(哔哩哔哩|bilibili|YouTube|优酷|爱奇艺|腾讯视频).*$',
        r'\s*[-–—]\s*\d+\s*$', r'^\*\s*', r'\s*\[.*?\]\s*$', r'\s*\(.*?\)\s*$',
        r'https?://\S+', r'www\.\S+', r'\.py\s*$', r'\.js\s*$', r'\.html?\s*$',
        r'\.css\s*$', r'\.md\s*$', r'\.txt\s*$', r'\.json\s*$',
    ]
    cleaned = title
    for pattern in patterns_to_remove:
        cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
    cleaned = ' '.join(cleaned.split())
    if len(cleaned) < 3:
        parts = re.split(r'\s*[-–—|]\s*', title)
        if parts and len(parts[0]) >= 3:
            cleaned = parts[0].strip()
    return cleaned[:100]

async def search_google(query: str, limit: int = 10) -> Dict[str, Any]:
    try:
        if not query or len(query.strip()) < 2: return {'success': False, 'error': '搜索关键词太短'}
        query = query.strip()
        url = f"https://www.google.com/search?q={quote(query)}&hl=en"
        headers = {
            'User-Agent': get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9', 'Connection': 'keep-alive', 'DNT': '1'
        }
        await asyncio.sleep(random.uniform(0.2, 0.5))
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            results = parse_google_results(response.text, limit)
            if results: return {'success': True, 'query': query, 'results': results}
            else: return {'success': False, 'error': '未能解析到搜索结果', 'query': query}
    except Exception as e: return {'success': False, 'error': str(e)}

def parse_google_results(html_content: str, limit: int = 5) -> List[Dict[str, str]]:
    results = []
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        result_divs = soup.find_all('div', class_='g')
        for div in result_divs[:limit * 2]:
            link = div.find('a')
            if link:
                h3 = div.find('h3')
                title = h3.get_text(strip=True) if h3 else link.get_text(strip=True)
                if title and 3 < len(title) < 200:
                    href = link.get('href', '')
                    if href:
                        if href.startswith('/url?'): url = parse_qs(urlparse(href).query).get('q', [href])[0]
                        elif href.startswith('http'): url = href
                        else: url = urljoin('https://www.google.com', href)
                    else: url = ''
                    abstract = ""
                    snippet_div = div.find('div', class_=lambda x: x and ('VwiC3b' in x if x else False))
                    if snippet_div: abstract = snippet_div.get_text(strip=True)[:200]
                    else:
                        for span in div.find_all('span'):
                            if len(span.get_text(strip=True)) > 50:
                                abstract = span.get_text(strip=True)[:200]
                                break
                    if not any(skip in title.lower() for skip in ['ad', 'sponsored', 'javascript']):
                        results.append({'title': title, 'abstract': abstract, 'url': url})
                        if len(results) >= limit: break
        logger.info(f"解析到 {len(results)} 条Google搜索结果")
        return results[:limit]
    except Exception as e:
        logger.exception(f"解析Google搜索结果失败: {e}")
        return []

async def search_baidu(query: str, limit: int = 5) -> Dict[str, Any]:
    try:
        if not query or len(query.strip()) < 2: return {'success': False, 'error': '搜索关键词太短'}
        query = query.strip()
        url = f"https://www.baidu.com/s?wd={quote(query)}"
        headers = {
            'User-Agent': get_random_user_agent(), 'Referer': 'https://www.baidu.com/',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8', 'Connection': 'keep-alive', 'DNT': '1'
        }
        await asyncio.sleep(random.uniform(0.2, 0.5))
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            results = parse_baidu_results(response.text, limit)
            if results: return {'success': True, 'query': query, 'results': results}
            else: return {'success': False, 'error': '未能解析到搜索结果', 'query': query}
    except Exception as e: return {'success': False, 'error': str(e)}

def parse_baidu_results(html_content: str, limit: int = 5) -> List[Dict[str, str]]:
    results = []
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        containers = soup.find_all('div', class_=lambda x: x and 'c-container' in x, limit=limit * 2)
        for container in containers:
            link = container.find('a')
            if link:
                title = link.get_text(strip=True)
                if title and 5 < len(title) < 200:
                    href = link.get('href', '')
                    if href:
                        if href.startswith('/'): url = urljoin('https://www.baidu.com', href)
                        elif not href.startswith('http'): url = urljoin('https://www.baidu.com/', href)
                        else: url = href
                    else: url = ''
                    abstract = ""
                    content_span = container.find('span', class_=lambda x: x and 'content-right' in x)
                    if content_span: abstract = content_span.get_text(strip=True)[:200]
                    if not any(skip in title.lower() for skip in ['百度', '广告', 'javascript']):
                        results.append({'title': title, 'abstract': abstract, 'url': url})
                        if len(results) >= limit: break
        
        if not results:
            for h3 in soup.find_all('h3')[:limit]:
                link = h3.find('a')
                if link:
                    title = link.get_text(strip=True)
                    if title and 5 < len(title) < 200:
                        href = link.get('href', '')
                        if href:
                            if href.startswith('/'): url = urljoin('https://www.baidu.com', href)
                            elif not href.startswith('http'): url = urljoin('https://www.baidu.com/', href)
                            else: url = href
                        else: url = ''
                        results.append({'title': title, 'abstract': '', 'url': url})
        logger.info(f"解析到 {len(results)} 条百度搜索结果")
        return results[:limit]
    except Exception as e:
        logger.exception(f"解析百度搜索结果失败: {e}")
        return []

def format_baidu_search_results(search_result: Dict[str, Any]) -> str:
    if not search_result.get('success'): return f"搜索失败: {search_result.get('error', '未知错误')}"
    output_lines = [f"【关于「{search_result.get('query', '')}」的搜索结果】\n"]
    for i, result in enumerate(search_result.get('results', []), 1):
        output_lines.append(f"{i}. {result.get('title', '')}")
        if result.get('abstract'): output_lines.append(f"   {result['abstract'][:150]}...")
        output_lines.append("")
    return "\n".join(output_lines) if search_result.get('results') else "未找到相关结果"

def format_search_results(search_result: Dict[str, Any]) -> str:
    china_region = is_china_region()
    if not search_result.get('success'):
        return f"搜索失败: {search_result.get('error', '未知错误')}" if china_region else f"Search failed: {search_result.get('error', 'Unknown error')}"
    
    query = search_result.get('query', '')
    output_lines = [f"【关于「{query}」的搜索结果】\n" if china_region else f"【Search results for「{query}」】\n"]
    for i, result in enumerate(search_result.get('results', []), 1):
        output_lines.append(f"{i}. {result.get('title', '')}")
        if result.get('abstract'): output_lines.append(f"   {result['abstract'][:150]}...")
        output_lines.append("")
    if not search_result.get('results'): output_lines.append("未找到相关结果" if china_region else "No results found")
    return "\n".join(output_lines)

async def fetch_window_context_content(limit: int = 5) -> Dict[str, Any]:
    try:
        china_region = is_china_region()
        title_result = get_active_window_title(include_raw=True)
        if not title_result or isinstance(title_result, str): return {'success': False, 'error': '无法获取当前活跃窗口标题'}
        
        sanitized_title = title_result['sanitized']
        search_queries = await generate_diverse_queries(clean_window_title(title_result['raw']))
        
        if not search_queries or all(not q or len(q) < 2 for q in search_queries):
            return {'success': False, 'error': '无法提取有效搜索关键词', 'window_title': sanitized_title}
        
        all_results, successful_queries = [], []
        search_func = search_baidu if china_region else search_google
        
        # 【修改并发代码片段】
        valid_queries = [q for q in search_queries if q and len(q) >= 2]
        if not valid_queries:
            return {'success': False, 'error': '无法提取有效搜索关键词', 'window_title': sanitized_title}
        
        # 将所有搜索请求打包为并发任务，破除串行瓶颈
        tasks = [search_func(query, limit) for query in valid_queries]
        gather_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for query, search_result in zip(valid_queries, gather_results):
            if isinstance(search_result, dict) and search_result.get('success') and search_result.get('results'):
                all_results.extend(search_result['results'])
                successful_queries.append(query)
        
        seen_keys, unique_results = set(), []
        for result in all_results:
            key = result.get('url') or result.get('title')
            if key and key not in seen_keys:
                seen_keys.add(key)
                unique_results.append(result)
        
        unique_results = unique_results[:limit * 2]
        if not unique_results: return {'success': False, 'error': '所有查询均未获得搜索结果', 'window_title': sanitized_title, 'search_queries': search_queries}
        
        return {
            'success': True, 'window_title': sanitized_title, 'search_queries': successful_queries,
            'search_results': unique_results, 'region': 'china' if china_region else 'non-china'
        }
    except Exception as e: return {'success': False, 'error': str(e)}

def format_window_context_content(content: Dict[str, Any]) -> str:
    china_region = is_china_region()
    if not content.get('success'):
        return f"获取窗口上下文失败: {content.get('error', '未知错误')}" if china_region else f"Failed to fetch window context: {content.get('error', 'Unknown error')}"
    
    output_lines = []
    window_title = content.get('window_title', '')
    search_queries = content.get('search_queries', [])
    results = content.get('search_results', [])
    
    if china_region:
        output_lines.append(f"【当前活跃窗口】{window_title}")
        if search_queries: output_lines.append(f"【搜索关键词】{', '.join(search_queries)}")
        output_lines.extend(["", "【相关信息】"])
    else:
        output_lines.append(f"【Active Window】{window_title}")
        if search_queries: output_lines.append(f"【Search Keywords】{', '.join(search_queries)}")
        output_lines.extend(["", "【Related Information】"])
    
    for i, result in enumerate(results, 1):
        output_lines.append(f"{i}. {result.get('title', '')}")
        if result.get('abstract'): output_lines.append(f"   {result.get('abstract', '')[:150]}...")
        if result.get('url'): output_lines.append(f"   链接: {result.get('url', '')}" if china_region else f"   Link: {result.get('url', '')}")
    
    if not results: output_lines.append("未找到相关信息" if china_region else "No related information found")
    return "\n".join(output_lines)

# =========================================
# 
# =========================================
# ==========================================
# 凭证与 Cookie 获取模块
# ==========================================
def _get_platform_cookies(platform_name: str) -> dict[str, str]:
    """
    通用平台 Cookie 读取器 (接入系统底层的加密/明文统一读取逻辑)
    """
    try:
        # 优先调用系统底层的解密读取逻辑
        from utils.cookies_login import load_cookies_from_file
        cookies = load_cookies_from_file(platform_name)
        if cookies:
            logger.debug(f"✅ 成功通过底层接口加载 {platform_name} 凭证")
            return cookies
    except Exception as e:
        logger.debug(f"底层接口加载 {platform_name} 凭证失败: {e}，尝试使用明文回退...")

    # 下面是作为回退的明文读取逻辑（兜底处理旧文件）
    possible_paths = [
        Path(os.path.expanduser('~')) / f'{platform_name}_cookies.json',
        Path('config') / f'{platform_name}_cookies.json',
        Path('.') / f'{platform_name}_cookies.json',
    ]
    
    for cookie_file in possible_paths:
        if not cookie_file.exists():
            continue
            
        try:
            with open(cookie_file, 'r', encoding='utf-8') as f:
                cookie_data = json.load(f)

            cookies = {}
            if isinstance(cookie_data, list):
                for cookie in cookie_data:
                    name, value = cookie.get('name'), cookie.get('value')
                    if name and value: 
                        cookies[name] = value
            elif isinstance(cookie_data, dict):
                cookies = cookie_data
            
            if cookies:
                return cookies
        except Exception:
            continue

    return {}

def _get_bilibili_credential() -> Any | None:
    try:
        from bilibili_api import Credential
        cookies = _get_platform_cookies('bilibili')
        if not cookies:
            return None
        
        # 兼容原版逻辑，加入 buvid3 防止被 B站 API 风控拦截
        return Credential(
            sessdata=cookies.get('SESSDATA', ''),
            bili_jct=cookies.get('bili_jct', ''),
            buvid3=cookies.get('buvid3', ''),
            dedeuserid=cookies.get('DedeUserID', '')
        )
    except ImportError:
        logger.debug("bilibili_api 库未安装")
        return None
    except Exception as e:
        logger.debug(f"从文件加载认证信息失败: {e}")
    
    return None


# ==========================================
#个人推荐/流 (Personal Dynamics - 需要用户认证信息)
# ==========================================

async def fetch_bilibili_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """
    获取B站推送的动态消息
    设计原则：
    - 仅需核心登录凭证 SESSDATA，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    import re

    try:
        credential = _get_bilibili_credential()
        if not credential: 
            return {'success': False, 'error': '未提供Bilibili认证信息'}

        url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
        headers = {"User-Agent": get_random_user_agent(), "Referer": "https://t.bilibili.com/"}
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, cookies=credential.get_cookies(), timeout=10.0)
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict) or data.get("code") != 0:
            logger.error(f"获取B站动态失败，API返回: {data}")
            return {'success': False, 'error': f"API请求失败"}

        def safe_dict(d: Any, key: str) -> dict:
            if not isinstance(d, dict): return {}
            v = d.get(key)
            return v if isinstance(v, dict) else {}

        dynamic_list = []
        items = data.get("data")
        items = items.get("items", []) if isinstance(items, dict) else []

        for item in items:
            if not isinstance(item, dict): continue
                
            try:
                dynamic_id = str(item.get("id_str", ""))
                dynamic_type = str(item.get("type", ""))
                if dynamic_type in {"DYNAMIC_TYPE_AD", "DYNAMIC_TYPE_APPLET", "DYNAMIC_TYPE_NONE"}: 
                    continue
                
                modules = safe_dict(item, "modules")
                module_author = safe_dict(modules, "module_author")
                
                # 获取到了作者名
                author = module_author.get("name") or "未知UP主"
                pub_time = module_author.get("pub_time") or "刚刚"
                
                module_dynamic = safe_dict(modules, "module_dynamic")
                major = safe_dict(module_dynamic, "major")
                desc = safe_dict(module_dynamic, "desc")
                
                major_type = major.get("type")
                raw_text = desc.get("text") or ""
                
                content = ""
                specific_url = f"https://t.bilibili.com/{dynamic_id}"  # 默认动态页面URL
                
                match major_type:
                    case "MAJOR_TYPE_ARCHIVE": 
                        # 视频动态：添加视频链接
                        archive = safe_dict(major, "archive")
                        bvid = archive.get("bvid", "")
                        if bvid:
                            specific_url = f"https://www.bilibili.com/video/{bvid}"
                        content = f"[发布了新视频] {archive.get('title', '')}"
                        
                    case "MAJOR_TYPE_DRAW": 
                        # 图文动态：保持动态页面链接
                        content = f"[图文动态] {raw_text}" if raw_text else "[分享了图片]"
                        
                    case "MAJOR_TYPE_ARTICLE":
                        # 专栏文章：添加文章链接
                        article = safe_dict(major, "article")
                        article_id = article.get("id", "")
                        if article_id:
                            specific_url = f"https://www.bilibili.com/read/cv{article_id}"
                        content = f"[发布了专栏文章] {article.get('title', '')}"
                        
                    case "MAJOR_TYPE_LIVE_RCMD":
                        # 直播动态：添加直播间链接
                        live_title = raw_text
                        try:
                            live_rcmd = major.get("live_rcmd") or major.get("live")
                            if isinstance(live_rcmd, dict):
                                content_str = live_rcmd.get("content")
                                if isinstance(content_str, str) and content_str.startswith("{"):
                                    play_info = json.loads(content_str).get("live_play_info")
                                    if isinstance(play_info, dict):
                                        live_title = play_info.get("title", live_title)
                                        room_id = play_info.get("room_id")
                                        if room_id:
                                            specific_url = f"https://live.bilibili.com/{room_id}"
                                elif isinstance(live_rcmd.get("live_play_info"), dict):
                                    live_title = live_rcmd["live_play_info"].get("title", live_title)
                                    room_id = live_rcmd["live_play_info"].get("room_id")
                                    if room_id:
                                        specific_url = f"https://live.bilibili.com/{room_id}"
                        except Exception: pass
                        content = f"[正在直播] {live_title or '快来我的直播间看看吧！'}"
                        
                    case _:
                        if dynamic_type == "DYNAMIC_TYPE_LIVE_RCMD":
                            # 直播开播推送：添加直播间链接
                            content = f"[正在直播] {raw_text or '快来我的直播间看看吧！'}"
                            # 尝试从描述中提取直播间ID
                            import re
                            room_match = re.search(r'直播间：(\d+)', raw_text)
                            if room_match:
                                specific_url = f"https://live.bilibili.com/{room_match.group(1)}"
                                
                        elif dynamic_type == "DYNAMIC_TYPE_FORWARD":
                            content = f"[转发动态] {raw_text}" if raw_text else "[转发了动态]"
                        else:
                            content = raw_text or "发布了新动态"

                content = re.sub(r'\s+', ' ', content).strip()
                if not content: content = "分享了新动态"

                final_content = f"UP主【{author}】: {content}"

                dynamic_list.append({
                    'dynamic_id': dynamic_id, 'type': dynamic_type, 'timestamp': pub_time,
                    'author': author, 'content': final_content,  # 存入拼接好的完整字符串
                    'url': specific_url,  # 使用具体类型的URL
                    'base_url': f"https://t.bilibili.com/{dynamic_id}"  # 保留原始动态页面链接
                })
                if len(dynamic_list) >= limit: break
            except Exception as item_e:
                logger.warning(f"解析单条动态失败，跳过: {item_e}, 动态ID: {item.get('id_str', '未知')}")

        if dynamic_list:
            logger.info(f"✅ 成功获取到 {len(dynamic_list)} 条你关注的UP主动态消息")
        return {'success': True, 'dynamics': dynamic_list}

    except Exception as e:
        logger.error(f"获取B站动态消息失败: {e}")
        return {'success': False, 'error': str(e)}
        
async def fetch_douyin_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    pass

async def fetch_kuaishou_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """获取快手个人关注动态 (GraphQL 接口 + 严格 Cookie)"""
    pass

async def fetch_weibo_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """
    获取微博动态
    设计原则：
    - 切换至 Mobile 移动版 API，彻底绕过 PC 端所有风控
    - 仅需核心登录凭证 SUB，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    import re
    import random
    import asyncio
    import httpx
    
    try:
        weibo_cookies = _get_platform_cookies('weibo')
        if not weibo_cookies:
            return {'success': False, 'error': '未找到 config/weibo_cookies.json'}
        
        # 1. 只需要最核心的 SUB，其他全都不需要！
        sub = weibo_cookies.get('SUB') or weibo_cookies.get('sub')
        if not sub:
            logger.error("❌ 缺少核心登录凭证 SUB。")
            return {'success': False, 'error': '缺少核心登录凭证 SUB'}

        # 2. 目标变更为：移动端首页关注流的固定 Container ID
        url = "https://m.weibo.cn/api/container/getIndex?containerid=102803"
        
        # 3. 必须伪装成手机浏览器的 User-Agent
        mobile_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
        
        headers = {
            'User-Agent': mobile_ua,
            'Referer': 'https://m.weibo.cn/',
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
            'MWeibo-Pwa': '1'
        }
        
        # 仅携带最纯净的 SUB 即可
        req_cookies = {'SUB': sub}
        
        await asyncio.sleep(random.uniform(0.1, 0.5))

        # 4. 移动端 API 非常宽容，直接用普通的 httpx 即可稳定发包
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, cookies=req_cookies)
            
            if response.status_code != 200:
                logger.error(f"❌ 移动端微博接口异常，状态码: {response.status_code}")
                return {'success': False, 'error': f"API请求失败，状态码: {response.status_code}"}
                
            data = response.json()
            
            # 移动端如果未登录，通常会返回 ok: 0 或者重定向
            if data.get('ok') != 1:
                logger.error("❌ 微博拦截：返回 ok=0，说明你的 SUB 凭证已过期！")
                return {'success': False, 'error': "微博凭证已过期，请去浏览器重新获取"}
            
            cards = data.get('data', {}).get('cards', [])
            weibo_list = []
            
            for card in cards:
                # card_type == 9 代表这是一条正常的微博博文卡片
                if card.get('card_type') != 9:
                    continue
                    
                mblog = card.get('mblog')
                if not mblog:
                    continue
                    
                user = mblog.get('user', {})
                author = user.get('screen_name') or '未知博主'
                
                # 提取正文并清理 HTML 标签
                text = str(mblog.get('text') or '')
                clean_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip()
                
                # 兼容并缝合转发内容
                if mblog.get('retweeted_status'):
                    retweet = mblog['retweeted_status']
                    rt_author = retweet.get('user', {}).get('screen_name') or '原博主'
                    rt_text = str(retweet.get('text') or '')
                    rt_clean_text = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', rt_text)).strip()
                    clean_text = f"{clean_text} // [转发动态] @{rt_author}: {rt_clean_text}"
                
                display_text = clean_text if clean_text else "[分享了图片/动态]"
                final_content = f"博主【{author}】: {display_text}"
                mid = mblog.get('mid') or mblog.get('id', '')
                
                weibo_list.append({
                    'author': author,
                    'content': final_content,
                    'timestamp': mblog.get('created_at') or '',
                    'url': f"https://m.weibo.cn/detail/{mid}" # 使用移动端 URL
                })
                
                if len(weibo_list) >= limit:
                    break

            if weibo_list: 
                logger.info(f"✅ 成功通过移动端接口获取到 {len(weibo_list)} 条微博个人动态")
                logger.info("微博动态:")  # 统一对齐 B站 的提示词
                for i, weibo in enumerate(weibo_list, 1):
                    content = weibo.get('content', '')
                    # 稍微放宽一点截断长度，保证显示效果更好
                    if len(content) > 50:
                        content = content[:50] + "..."
                    # 去掉冗余的时间和作者，直接干干净净地打印 content
                    logger.info(f"  - {content}")
                
                return {'success': True, 'statuses': weibo_list}
            else:
                return {'success': False, 'error': '未解析到微博内容'}
                
    except Exception as e: 
        logger.error(f"微博动态解析发生错误: {e}")
        return {'success': False, 'error': str(e)}

async def fetch_reddit_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """
    获取Reddit推送的动态帖子
    设计原则：
    - 仅需核心登录凭证 reddit_session，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    try:
        reddit_cookies = _get_platform_cookies('reddit')
        if not reddit_cookies: 
            return {'success': False, 'error': '未配置 config/reddit_cookies.json'}
        url = f"https://www.reddit.com/hot.json?limit={limit}"
        headers = {'User-Agent': get_random_user_agent(), 'Accept': 'application/json'}
        await asyncio.sleep(random.uniform(0.1, 0.5))

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, cookies=reddit_cookies)
            data = response.json()
            posts = [
                {
                    'title': pd.get('title', ''), 'subreddit': f"r/{pd.get('subreddit', '')}",
                    'score': _format_score(pd.get('score', 0)), 
                    'url': f"https://www.reddit.com{pd.get('permalink', '')}"
                }
                for item in data.get('data', {}).get('children', [])[:limit]
                if not (pd := item.get('data', {})).get('over_18')
            ]
            if posts: logger.info(f"✅ 成功获取到 {len(posts)} 条Reddit订阅帖子")
            return {'success': True, 'posts': posts}
    except Exception as e: 
        return {'success': False, 'error': str(e)}


async def _fetch_twitter_personal_web_scraping(limit: int = 10, cookies: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Twitter 网页抓取 fallback
    设计原则：
    - 仅需核心登录凭证 twitter_session，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    try:
        url = "https://twitter.com/home"
        headers = {'User-Agent': get_random_user_agent()}
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            res = await client.get(url, headers=headers, cookies=cookies)
            
            # 如果被重定向到了登录页，说明 Cookie 彻底失效了
            if "login" in str(res.url) or "logout" in str(res.url):
                return {'success': False, 'error': 'Twitter Cookie 已过期，网页端拒绝访问'}
                
            tweets = []
            tweet_texts = re.findall(r'"tweet":\{[^}]*"full_text":"([^"]+)"', res.text)
            screen_names = re.findall(r'"screen_name":"([^"]+)"', res.text)
            
            for i, text in enumerate(tweet_texts[:limit]):
                clean_text = re.sub(r'https://t\.co/\w+', '', text).strip()
                tweets.append({
                    'author': f"@{screen_names[i] if i<len(screen_names) else 'Unknown'}", 
                    'content': clean_text,
                    'timestamp': '刚刚'  # 保持与主 API 数据字典格式的统一
                })
                
            return {'success': True, 'tweets': tweets} if tweets else {'success': False, 'error': '网页正则抓取失败，页面结构可能已变更'}
    except Exception as e: 
        logger.error(f"Twitter 网页抓取 fallback 失败: {e}")
        return {'success': False, 'error': str(e)}

async def fetch_twitter_personal_dynamic(limit: int = 10) -> Dict[str, Any]:
    """
    获取 Twitter 个人时间线
    设计原则：
    - 仅需核心登录凭证 twitter_session，其他 Cookie 全部失效
    - 目标变更为：移动端首页关注流的固定 Container ID
    - 必须伪装成手机浏览器的 User-Agent
    """
    
    try:
        twitter_cookies = _get_platform_cookies('twitter')
        if not twitter_cookies:
             return {'success': False, 'error': '未配置 config/twitter_cookies.json'}
             
        # 提取防伪 CSRF Token。Twitter 必须，否则哪怕有合法 Cookie 也会立刻 401/403
        ct0 = twitter_cookies.get('ct0') or twitter_cookies.get('CT0', '')
        if not ct0:
            logger.warning("Twitter Cookie 中缺少核心字段 ct0，极大可能触发风控拦截")
        
        # 官方 Web 客户端通用固化的 Bearer Token
        bearer_token = 'AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIyU2%2FGoa3FmBNYDPz%2FzGz%2F2Rnc%2F2bGBDH%2Fc'
        
        # 切换到更稳定、包含完整推文文本的 v1.1 接口
        url = f"https://api.twitter.com/1.1/statuses/home_timeline.json?tweet_mode=extended&count={limit}"
        
        # 补全极其严格的 Twitter 风控协议头
        headers = {
            'User-Agent': get_random_user_agent(), 
            'Accept': 'application/json',
            'Authorization': f'Bearer {bearer_token}',
            'x-twitter-auth-type': 'OAuth2Session' if 'auth_token' in twitter_cookies else '',
            'x-csrf-token': ct0,  # <-- 防火墙放行的关键钥匙
            'x-twitter-active-user': 'yes',
            'x-twitter-client-language': 'zh-cn'
        }
        
        await asyncio.sleep(random.uniform(0.1, 0.5))

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, cookies=twitter_cookies)
            
            # 状态码非 200 时，平滑降级到备用网页刮削方案
            if response.status_code != 200: 
                logger.warning(f"Twitter API 拒绝访问 (状态码: {response.status_code})，回退到网页刮削...")
                return await _fetch_twitter_personal_web_scraping(limit, twitter_cookies)
                
            # 真正去解析返回的推文数据，替换掉之前的占位符
            data = response.json()
            if not isinstance(data, list):
                return {'success': False, 'error': 'API 返回数据格式异常'}
                
            tweets = []
            for tweet in data[:limit]:
                user = tweet.get('user', {})
                author = user.get('screen_name') or 'Unknown'
                # tweet_mode=extended 时，正文在 full_text 里
                text = str(tweet.get('full_text') or tweet.get('text') or '')
                
                # 清理推文末尾自带的分享短链接 (https://t.co/xxx)
                clean_text = re.sub(r'https://t\.co/\w+', '', text).strip()
                
                # 处理转推 (Retweet) 的前缀拼接
                if 'retweeted_status' in tweet:
                    rt_user = tweet['retweeted_status'].get('user', {}).get('screen_name', 'Unknown')
                    rt_text = str(tweet['retweeted_status'].get('full_text') or '')
                    rt_clean_text = re.sub(r'https://t\.co/\w+', '', rt_text).strip()
                    clean_text = f"RT @{rt_user}: {rt_clean_text}"
                
                tweets.append({
                    'author': f"@{author}", 
                    'content': clean_text,
                    'timestamp': tweet.get('created_at', '')
                })
                
            if tweets:
                logger.info(f"✅ 成功获取到 {len(tweets)} 条 Twitter 个人时间线动态")
                return {'success': True, 'tweets': tweets}
            else:
                return {'success': False, 'error': '未解析到推文内容'}
                
    except Exception as e: 
        logger.error(f"Twitter API 获取失败: {e}")
        return {'success': False, 'error': str(e)}

async def fetch_personal_dynamics(limit: int = 10) -> Dict[str, Any]:
    """
    独立获取全平台个人登录态下的订阅/关注动态
    """
    try:
        china_region = is_china_region()
        if china_region:
            logger.info("检测到中文区域，获取B站和微博个人动态")
            b_dyn, w_dyn = await asyncio.gather(
                fetch_bilibili_personal_dynamic(limit),
                fetch_weibo_personal_dynamic(limit),
                return_exceptions=True
            )
            # 异常隔离与安全降级
            b_dyn = {'success': False, 'error': str(b_dyn)} if isinstance(b_dyn, Exception) else b_dyn
            w_dyn = {'success': False, 'error': str(w_dyn)} if isinstance(w_dyn, Exception) else w_dyn

            return {'success': True, 'region': 'china', 
                    'bilibili_dynamic': b_dyn, 'weibo_dynamic': w_dyn, 
              }
        else:
            logger.info("检测到非中文区域，获取Reddit和Twitter个人动态")
            r_dyn, t_dyn = await asyncio.gather(
                fetch_reddit_personal_dynamic(limit),
                fetch_twitter_personal_dynamic(limit),
                return_exceptions=True
            )
            r_dyn = {'success': False, 'error': str(r_dyn)} if isinstance(r_dyn, Exception) else r_dyn
            t_dyn = {'success': False, 'error': str(t_dyn)} if isinstance(t_dyn, Exception) else t_dyn
            return {'success': True, 'region': 'non-china', 'reddit_dynamic': r_dyn, 'twitter_dynamic': t_dyn}
    except Exception as e:
        logger.error(f"获取个人动态内容失败: {e}")
        return {'success': False, 'error': str(e)}

def format_personal_dynamics(data: Dict[str, Any]) -> str:
    """格式化个人动态 (结构优化版：全配置表驱动 + 层级排版)"""
    output_lines = []
    region = data.get('region', 'china')
    
    if region == 'china':
        # 配置表：(数据字典键名, 展示标题, 列表的键名)
        platforms = [
            ('bilibili_dynamic', 'B站关注UP主动态', 'dynamics'),
            ('weibo_dynamic', '微博个人关注动态', 'statuses')
        ]
        
        for key, title, list_key in platforms:
            dyn_data = data.get(key, {})
            # 海象运算符 := 提取列表，如果为空则直接跳过该平台
            if dyn_data.get('success') and (items := dyn_data.get(list_key, [])):
                output_lines.append(f"【{title}】")
                
                for i, item in enumerate(items[:5], 1):
                    # 统一了排版结构，保证所有平台的缩进严格对齐 (3个空格)
                    author = item.get('author', '未知')
                    timestamp = item.get('timestamp', '')
                    content = item.get('content', '')
                    
                    output_lines.append(f"{i}. {author} ({timestamp})")
                    output_lines.append(f"   内容: {content}")
                    
                output_lines.append("") 
                
        return "\n".join(output_lines).strip() or "暂时无法获取关注动态"
        
    else:
        # 海外平台配置表
        platforms = [
            ('reddit_dynamic', 'Reddit Subscribed Posts', 'posts'),
            ('twitter_dynamic', 'Twitter Timeline', 'tweets')
        ]
        
        for key, title, list_key in platforms:
            dyn_data = data.get(key, {})
            if dyn_data.get('success') and (items := dyn_data.get(list_key, [])):
                output_lines.append(f"【{title}】")
                
                for i, item in enumerate(items[:5], 1):
                    if key == 'reddit_dynamic':
                        output_lines.append(f"{i}. {item.get('title')}")
                        output_lines.append(f"   Subreddit: {item.get('subreddit')} | Score: {item.get('score')} upvotes")
                    else:
                        output_lines.append(f"{i}. {item.get('author')}: {item.get('content')}")
                        
                output_lines.append("")
                
        return "\n".join(output_lines).strip() or "No personal timeline available"

# ==========================================
# 测试用的主函数
# ==========================================
async def main():
    """
    Web爬虫的测试函数
    自动检测区域并获取相应内容
    """
    china_region = is_china_region()
    
    if china_region:
        print("检测到中文区域")
        print("正在获取热门内容（B站、微博）...")
    else:
        print("检测到非中文区域")
        print("正在获取热门内容（Reddit、Twitter）...")
    
    # 测试提取分离后的公共热搜模块
    print("\n" + "="*50)
    trending_content = await fetch_public_content(bilibili_limit=5, weibo_limit=5, reddit_limit=5, twitter_limit=5)
    if trending_content['success']:
        print(format_public_content(trending_content))
    else:
        print(f"获取公共热点失败: {trending_content.get('error')}")
    print("="*50)

    # 测试提取分离后的个人订阅模块
    print("\n" + "="*50)
    personal_content = await fetch_personal_dynamics(limit=5)
    if personal_content['success']:
        print(format_personal_dynamics(personal_content))
    else:
        print(f"获取个人动态失败: {personal_content.get('error')}")
    print("="*50)

if __name__ == "__main__":
    asyncio.run(main())