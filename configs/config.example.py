#!/usr/bin/env python3
"""
WorkBuddy 统一管理器 - 采集器账号配置模板（config.example.py）
================================
本文件只被「积分采集器」wb_credits_fetcher.py 读取；API 网关与统一面板读的是
config.json（不存在时按内置默认值自动创建），网关不读本文件。

下面的 GATEWAY / CONSOLE / COLLECTOR / AUTOTASKS 是历史遗留示例常量，
当前没有任何代码读取它们（端口是各模块的常量），改它们不会改变行为。

首次使用：把本文件复制为 configs/config.py（该文件已被 .gitignore 忽略，不会上传），
再取消注释并填入真实账号。仓库里只保留这份全部注释掉的模板。
"""

# ===== 账号列表 =====
# 默认留空：填了这里才会被「积分采集器」(wb_credits_fetcher.py) 当成待采集账号。
# 留空时采集器会明确提示「没有配置账号」并退出（不再拿示例数据去扫码）。
ACCOUNTS = [
    # {
    #     "name": "工作号",                 # 别名，积分回填时按它匹配
    #     "phone": "139****9999",          # 显示用脱敏手机号
    #     "login_method": "wechat",        # wechat 或 phone
    # },
    # {
    #     "name": "小号",
    #     "phone": "137****7777",
    #     "login_method": "phone",
    #     "phone_number": "13712345678",   # phone 登录需填完整手机号
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

# ===== 自动任务配置（未使用：本分支没有调度器，见 README「定时与自动任务」）=====
AUTOTASKS = {
    "enabled": True,
    "checkin_times": ["09:00", "21:00"],  # 每日签到时间
    "balance_refresh_minutes": 5,  # 余额刷新间隔
}