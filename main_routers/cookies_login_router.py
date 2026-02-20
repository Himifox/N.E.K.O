# -*- coding: utf-8 -*-
"""
Cookies Login Router

Handles authentication-related endpoints including:
- Bilibili QR code login
- Manual cookie submission for various platforms
- Cookie management interface
"""

import json
import logging
import os
from typing import Dict, Optional

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

# 导入底层的认证逻辑
from utils.cookies_login import (
    PlatformLoginManager,
    save_cookies_to_file,
    load_cookies_from_file,
    parse_cookie_string
)

router = APIRouter(prefix="/api/auth", tags=["认证管理"])
logger = logging.getLogger("Main")

templates = Jinja2Templates(directory="templates")

login_manager = PlatformLoginManager()

# 接收前端手动提交 Cookie 的数据模型
import re
class CookieSubmit(BaseModel):
    # 限制平台名称长度，防止越权或路径遍历猜测
    platform: str = Field(..., min_length=2, max_length=20, description="平台名称")
    # 限制 Cookie 最大长度为 8192 字符（足够绝大多数正常 Cookie 使用，防止内存溢出攻击）
    cookie_string: str = Field(..., min_length=5, max_length=8192, description="Cookie字符串")
    encrypt: Optional[bool] = Field(False, description="是否加密存储 (仅适用于bilibili)")

class QRCodeRequest(BaseModel):
    platform: str = Field(..., description="平台名称 (目前仅支持bilibili)")

class QRCodeCheck(BaseModel):
    platform: str = Field(..., description="平台名称 (目前仅支持bilibili)")
    qrcode_key: str = Field(..., description="二维码密钥")

# ============ 1. 网页入口 ============

@router.get("/page", response_class=HTMLResponse, summary="凭证管理可视化后台入口")
async def render_auth_page(request: Request):
    """访问 http://你的IP:端口/api/auth/page 即可看到凭证管理网页"""
    return templates.TemplateResponse("cookies_login.html", {"request": request})

# ============ 2. 获取支持的平台 ============

@router.get("/platforms", summary="获取支持的平台列表")
async def get_supported_platforms():
    """获取所有支持的登录平台及其支持的登录方式"""
    try:
        platforms = login_manager.get_supported_platforms()
        return {
            "success": True,
            "data": {
                platform: {
                    "name": info["name"],
                    "methods": info["methods"],
                    "default_method": info["default_method"]
                }
                for platform, info in platforms.items()
            }
        }
    except Exception as e:
        logger.error(f"获取支持的平台失败: {e}")
        raise HTTPException(status_code=500, detail="获取支持的平台失败")


# ============ 4. 手动配置 API ============

@router.post("/cookies/save", summary="保存Cookie")
async def save_cookie(data: CookieSubmit):
    """处理手动Cookie提交"""
    try:
        # 如果检测到尖括号、脚本特征或异常控制字符，直接拒绝
        suspicious_pattern = re.compile(r'(<script|javascript:|onload=|eval\(|UNION SELECT|\.\./)', re.IGNORECASE)
        if suspicious_pattern.search(data.cookie_string):
            logger.warning(f"🚨 拦截到针对 {data.platform} 的恶意Cookie测试注入！")
            raise HTTPException(status_code=403, detail="检测到非法/危险字符，请求已被系统拦截。")
        # 验证平台是否支持
        platforms = login_manager.get_supported_platforms()
        if data.platform not in platforms:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {data.platform}")
            
        # 解析Cookie字符串
        cookies = parse_cookie_string(data.cookie_string)
        
        if not cookies:
            raise HTTPException(
                status_code=400, 
                detail="未提取到有效的 Cookie 键值对，请检查格式"
            )
        
        # 核心字段基础防呆校验
        platform_validations = {
            "bilibili": ["SESSDATA"],
            "douyin": ["sessionid", "ttwid"],
            "kuaishou": ["kuaishou.server.web_st", "userId"], 
            "weibo": ["SUB"],
            "twitter": ["auth_token"],
            "reddit": ["reddit_session"]  # 示例，实际字段可能不同
        }
        
        if data.platform in platform_validations:
            required_fields = platform_validations[data.platform]
            missing_fields = [field for field in required_fields if field not in cookies]
            
            if missing_fields:
                raise HTTPException(
                    status_code=400,
                    detail=f"格式错误：未检测到核心字段 {', '.join(missing_fields)}"
                )
        
        # 调用底层统一保存逻辑
        # 默认所有平台都加密，除非用户明确选择不加密
        encrypt = data.encrypt if data.encrypt is not None else True
        success = save_cookies_to_file(data.platform, cookies, encrypt=encrypt)
        
        if success:
            return {
                "success": True,
                "message": f"✅ {data.platform.capitalize()} 凭证已安全保存！",
                "data": {
                    "platform": data.platform,
                    "cookies_count": len(cookies),
                    "encrypted": encrypt
                }
            }
        else:
            raise HTTPException(
                status_code=500,
                detail="保存失败，请检查服务器目录权限"
            )
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"保存Cookie失败: {e}")
        raise HTTPException(status_code=500, detail="保存Cookie失败")

# ============ 5. Cookie管理 API ============

@router.get("/cookies/status", summary="检查所有平台Cookie状态")
async def get_all_cookies_status():
    """获取所有支持平台的Cookie状态"""
    try:
        platforms = login_manager.get_supported_platforms()
        result = {
            "success": True,
            "data": {
                "platforms": platforms,
            }
        }
        
        # 检查每个平台的cookies状态
        for platform in platforms:
            cookies = load_cookies_from_file(platform)
            result["data"][platform] = {
                "has_cookies": bool(cookies),
                "cookies_count": len(cookies) if cookies else 0
            }
        
        return result
    except Exception as e:
        logger.error(f"获取Cookie状态失败: {e}")
        raise HTTPException(status_code=500, detail="获取Cookie状态失败")

@router.get("/cookies/{platform}", summary="获取平台Cookie")
async def get_platform_cookies(platform: str):
    """获取指定平台的Cookie（仅返回基本信息，不返回敏感数据）"""
    try:
        # 验证平台是否支持
        platforms = login_manager.get_supported_platforms()
        if platform not in platforms:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {platform}")
            
        cookies = load_cookies_from_file(platform)
        
        if not cookies:
            return {
                "success": True,
                "data": {
                    "platform": platform,
                    "has_cookies": False,
                    "cookies_count": 0
                }
            }
            
        return {
            "success": True,
            "data": {
                "platform": platform,
                "has_cookies": True,
                "cookies_count": len(cookies),
                "cookie_names": list(cookies.keys())[:]  # 只返回前5个Cookie名称，不返回值
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取平台Cookie失败: {e}")
        raise HTTPException(status_code=500, detail="获取平台Cookie失败")

@router.delete("/cookies/{platform}", summary="删除平台Cookie")
async def delete_platform_cookies(platform: str):
    """删除指定平台的Cookie"""
    try:
        # 验证平台是否支持
        platforms = login_manager.get_supported_platforms()
        if platform not in platforms:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {platform}")
            
        from utils.cookies_login import COOKIE_FILES
        cookie_file = COOKIE_FILES.get(platform)
        
        if not cookie_file or not cookie_file.exists():
            return {
                "success": True,
                "message": f"{platform.capitalize()} Cookie文件不存在"
            }
            
        # 删除文件
        cookie_file.unlink()
        
        return {
            "success": True,
            "message": f"✅ {platform.capitalize()} Cookie已删除"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"删除平台Cookie失败: {e}")
        raise HTTPException(status_code=500, detail="删除平台Cookie失败")

# ============ 6. 兼容旧API ============

# 为了保持向后兼容，保留旧的API端点

@router.post("/save_cookie", summary="保存Cookie(兼容旧版)")
async def api_save_cookie(data: CookieSubmit):
    """兼容旧版本的Cookie保存API"""
    try:
        cookies = parse_cookie_string(data.cookie_string)
        
        if not cookies:
            return {"success": False, "msg": "未提取到有效的 Cookie 键值对，请检查格式。"}
            
        # 核心字段基础防呆校验
        if data.platform == "weibo" and "SUB" not in cookies:
            return {"success": False, "msg": "❌ 格式错误：未检测到核心字段 SUB"}
        if data.platform == "twitter" and "auth_token" not in cookies:
            return {"success": False, "msg": "❌ 格式错误：未检测到核心字段 auth_token"}
        if data.platform == "douyin" and "sessionid" not in cookies:
            return {"success": False, "msg": "❌ 格式错误：未检测到核心字段 sessionid"}
        if data.platform == "kuaishou" and "kuaishou.server.web_st" not in cookies:
            return {"success": False, "msg": "❌ 格式错误：未检测到核心字段 web_st"}
            
        # 调用底层统一保存逻辑
        success = save_cookies_to_file(data.platform, cookies)
        
        if success:
            return {"success": True, "msg": f"✅ {data.platform.capitalize()} 凭证已安全保存！"}
        else:
            return {"success": False, "msg": "❌ 保存失败，请检查服务器目录权限。"}
    except Exception as e:
        logger.error(f"保存Cookie失败: {e}")
        return {"success": False, "msg": f"❌ 保存失败: {str(e)}"}