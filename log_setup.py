#!/usr/bin/env python3
"""
log_setup.py — 统一日志配置模块

功能：
  - 同时输出到控制台和日志文件
  - 按日期自动轮转日志文件
  - 记录详细的 HTTP 请求信息（用于调试 403 等问题）
"""

import logging
import os
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler


def setup_logging(
    log_dir: str = "logs",
    log_level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,  # 5MB
    backup_count: int = 5,
    console_output: bool = True
) -> logging.Logger:
    """
    配置统一日志系统

    Args:
        log_dir: 日志文件目录
        log_level: 日志级别
        max_bytes: 单个日志文件最大大小
        backup_count: 保留的备份文件数量
        console_output: 是否同时输出到控制台

    Returns:
        配置好的 logger 实例
    """
    # 创建日志目录
    os.makedirs(log_dir, exist_ok=True)

    # 生成日志文件名（按日期）
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = os.path.join(log_dir, f"mcttk2rss_{today}.log")

    # 创建 logger
    logger = logging.getLogger("mcttk2rss")
    logger.setLevel(log_level)

    # 清除已有的处理器（避免重复添加）
    logger.handlers.clear()

    # 日志格式
    file_format = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_format = logging.Formatter(
        '[%(levelname)s] %(message)s'
    )

    # 文件处理器（带轮转）
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(file_format)
    logger.addHandler(file_handler)

    # 控制台处理器
    if console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(console_format)
        logger.addHandler(console_handler)

    return logger


def get_logger() -> logging.Logger:
    """获取已配置的 logger 实例"""
    return logging.getLogger("mcttk2rss")


# 便捷函数
def log_info(message: str):
    """记录信息级别日志"""
    get_logger().info(message)


def log_error(message: str, exc_info: bool = False):
    """记录错误级别日志"""
    get_logger().error(message, exc_info=exc_info)


def log_debug(message: str):
    """记录调试级别日志"""
    get_logger().debug(message)


def log_warning(message: str):
    """记录警告级别日志"""
    get_logger().warning(message)
