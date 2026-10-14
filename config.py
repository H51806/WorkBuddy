#!/usr/bin/env python3
"""
WorkBuddy 统一管理器 - 账号配置
================================
⚠️ 本文件包含敏感信息，请勿上传到公开仓库

格式：每个账号是一个 { ... } 块
"""

# ===== 账号列表 =====
ACCOUNTS = [
    {
        "name": "工作号",
        "phone": "139****9999",
        "login_method": "wechat",  # wechat 或 phone
    },
    # 添加更多账号：复制上面的块，修改 name 和 phone
    # {
    #     "name": "小号",
    #     "phone": "137****7777",
    #     "login_method": "phone",
    #     "phone_number": "13712345678",
    #     "password": "your_password",
    # },
]

# ===== 网关配置 =====
GATEWAY = {
    "api_key": "sk-CHANGEME-please-set-your-own-key",
    "port": 7863,
    "listen": "127.0.0.1",
}

# ===== 控制台配置 =====
CONSOLE = {
    "port": 8789,
    "refresh_interval": 30,  # 秒
}

# ===== 采集配置 =====
COLLECTOR = {
    "headless": False,  # True = 无头模式（后台运行）
    "browser_channel": "msedge",  # 使用 Edge 浏览器
}

# ===== 自动任务配置 =====
AUTOTASKS = {
    "enabled": True,
    "checkin_times": ["09:00", "21:00"],  # 每日签到时间
    "balance_refresh_minutes": 5,  # 余额刷新间隔
}