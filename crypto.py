#!/usr/bin/env python
# -*- coding: utf-8 -*-
import ccxt
import time
import json
import numpy as np
import pandas as pd
import tkinter as tk
import requests
import os
import akshare as ak
import threading
from datetime import datetime, timedelta
from threading import Thread, Event, Lock
from tkinter import ttk, scrolledtext, messagebox
from collections import deque
from typing import Callable, Optional, Dict, List, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from stock_utils import fetch_all_stock_codes
from threading import Event
from typing import Any, Dict, List, Tuple
import ctypes
import atexit

try:
    import talib
    TA_LIB_AVAILABLE = True
except ImportError:
    TA_LIB_AVAILABLE = False
#===========================================================
def fetch_latest_stock_price(symbol):
    """从腾讯/新浪获取最新股票价格，防止 AkShare 数据延迟"""
    url = f"https://qt.gtimg.cn/q={symbol}"  # 腾讯行情接口
    try:
        response = requests.get(url, timeout=5)
        response.encoding = "gbk"
        data = response.text.split("~")
        if len(data) > 10:
            latest_price = float(data[3])  # 最新价格
            latest_time = data[30]  # 最新时间
            return latest_price, latest_time
    except Exception as e:
        print(f"{symbol} 获取最新价格失败: {e}")
    return None, None

def fetch_stock_data(symbol, period="5min", count=200):
        """
        根据 stock_period 获取数据，并预加载更多历史数据。
        preload_days: 预加载的历史数据天数
        """
        import akshare as ak  # **延迟导入**
        import pandas as pd
        import requests

        try:
            df = ak.stock_zh_a_minute(symbol=symbol, period=period, adjust="qfq")
            if df is None or df.empty:
                return None
        
            latest_price, latest_time = fetch_latest_stock_price(symbol)
            if latest_price:
                latest_row = {
                    "时间": latest_time,
                    "开盘": latest_price,
                    "最高": latest_price,
                    "最低": latest_price,
                    "收盘": latest_price,
                    "成交量": 0  # 无法获取实时成交量
                }
                df = pd.concat([df, pd.DataFrame([latest_row])], ignore_index=True) # **用 pd.concat 代替 append**
            df["收盘"] = pd.to_numeric(df["收盘"])
            return df.tail(count)
        except Exception as e:
            print(f"{symbol} 获取数据异常: {e}")
            return None
# ======================== 警报历史记录 ========================
class AlertHistory:
    """用于记录并查询历史警报信息（线程安全）"""

    def __init__(self, max_records: int = 1000, log_callback: Optional[Callable] = None):
        self.records: deque = deque(maxlen=max_records)
        self.lock = Lock()
        self.stats = {
            "bullish_signals": 0,  # 多头信号数
            "bearish_signals": 0   # 空头信号数
        }
        self.log_callback = log_callback  # 新增日志回调

    def add_record(self, record: dict) -> None:
        """添加一条警报记录"""
        record['timeframe'] = self.config.get("price_timeframe", "5m")  # 获取最新时间参数
        with self.lock:
            self.records.append(record)
            self.log(f"📜 记录新历史: {record}", "log")
            if self.log_callback:  # 通过回调记录日志
                self.log_callback(f"⚠️ 记录警报: {record}", "log")
            if record['type'] == 'bullish':
                self.stats["bullish_signals"] += 1
            elif record['type'] == 'bearish':
                self.stats["bearish_signals"] += 1
    
    def get_stats(self) -> Dict[str, int]:
        """获取当前的监控统计信息"""
        with self.lock:
            return self.stats.copy()

    def get_records(self, start_time: datetime = None,
                    end_time: datetime = None,
                    alert_type: str = None) -> List[dict]:
        """按条件筛选历史记录"""
        with self.lock:
            filtered = []
            for r in self.records:
                timestamp = datetime.strptime(r['timestamp'], '%Y-%m-%d %H:%M:%S')
                if start_time and timestamp < start_time:
                    continue
                if end_time and timestamp > end_time:
                    continue
                if alert_type and r['type'] != alert_type:
                    continue
                filtered.append(r)
            return filtered


# ======================== Telegram通知 ========================
class TelegramNotifier:
    """用于发送Telegram通知"""

    def __init__(self, token: str, chat_ids: list):
        self.base_url = f"https://api.telegram.org/bot{token}/sendMessage"
        # chat_ids 现在可以是一个包含多个聊天ID的列表
        self.chat_ids = chat_ids if isinstance(chat_ids, list) else [chat_ids]
        self.enabled = False

    def test_connection(self) -> bool:
        """测试连接，发送测试消息"""
        try:
            return self._send_telegram_message({'text': '📡 监控系统连接测试成功'})
        except Exception:
            return False

    def send_message(self, message: str) -> bool:
        """发送消息到Telegram"""
        if not self.webhook_url:  # 检查配置有效性
            return False
        if not self.enabled:
            return False
        return self._send_telegram_message({'text': message})

    def _send_telegram_message(self, params: dict) -> bool:
        """内部方法，尝试多次发送消息"""
        params.update({
            'parse_mode': 'Markdown'
        })
        for chat_id in self.chat_ids:
            params['chat_id'] = chat_id
            for _ in range(3):
                try:
                    response = requests.post(self.base_url, params=params, timeout=5)
                    response.raise_for_status()
                    break  # 如果消息发送成功，停止重试
                except Exception:
                    time.sleep(2)
            else:
                # 如果尝试了3次都失败，返回False
                return False
        return True
# ======================== 企业微信通知 ========================
class EnterpriseWeChatNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self.enabled = False

    def test_connection(self) -> bool:
        """发送测试消息，检查 webhook 是否可用"""
        return self.send_message("【测试】企业微信提醒连接测试成功")

    def send_message(self, message: str) -> bool:
        payload = {
            "msgtype": "text",
            "text": {
                "content": message
            }
        }
        try:
            response = requests.post(self.webhook_url, json=payload, timeout=5)
            response.raise_for_status()
            # 根据企业微信返回信息判断是否成功
            result = response.json()
            if result.get("errcode") == 0:
                return True
        except Exception as e:
            print("企业微信发送异常:", e)
        return False


# ======================== 内存清理线程 ========================
class MemoryOptimizer(Thread):
    """定期清理缓存与历史数据，避免内存过高"""

    def __init__(self, monitor, interval: int = 600):
        super().__init__(daemon=True)
        self.monitor = monitor
        self.running = True
        self.interval = interval

    def run(self) -> None:
        while self.running:
            self.monitor.cleanup_data()
            time.sleep(self.interval)
    
    def stop(self):
        self.running = False

# 全局辅助函数
def clean_symbol(symbol):
    # 如果 symbol 中出现多次 USDT，则保留第一次出现前的部分+"/USDT"
    if symbol.count("USDT") > 1:
        base = symbol.split("/")[0]
        return f"{base}/USDT"
    return symbol
# ======================== 监控核心类 ========================
class CryptoMonitorPro:
    """
    核心监控类：
      - 初始化交易所连接
      - 定时获取K线数据、价格数据
      - 分析价格变化及均线策略（支持多组均线策略）
      - 发送警报（包含Telegram通知与日志记录）
    """

    def __init__(self, config: Dict, log_callback: Optional[Callable] = None):
        self.config = config
        self.log_callback = log_callback
        self.running = Event()  # 这里用 Event 对象替代布尔值
        self.running.set()       # 设置为运行状态
        self.init_complete = Event()
        # 初始配置数据
        self.price_data: Dict[str, dict] = {}
        self.base_prices: Dict[str, float] = {}  # 新增：记录每个交易对的基准价格
        self.single_pair_strategies = {}  # 用于存储单对监控配置，key 为交易对代码
        self.exchanges: Dict[str, ccxt.Exchange] = {}
        self.exchange_status: Dict[str, str] = {}
        self.last_leaderboard_log_time = 0 
        # 定义冷却时间，单位为秒（例如 300 秒即 5 分钟）
        self.alert_cooldown = self.config.get("alert_cooldown", 6000)
        self.last_alert_time: Dict[Tuple[str, str], float] = {}  # 存储每个 (symbol, alert_type) 的上次提醒时间

        


        # 数据缓存与统计
        self.market_cache: Dict[str, dict] = {}
        self.ma_data: Dict[str, dict] = {}
        self.usdt_pairs_cache: Dict[str, List[str]] = {}
        self.data_lock = Lock()
        self.stats_lock = Lock()
        self.connection_stats = {
            'total_pairs': 0,
            'success_pairs': 0,
            'last_update': None
        }
        # 修改历史记录初始化
        self.history = AlertHistory(log_callback=log_callback)  # 传递日志回调

        # 通知类（Telegram）
        self.notifier = TelegramNotifier(config['tg_token'], config['tg_chat_id'])
        if config.get('enable_tg'):
            self.notifier.enabled = True
            self.notifier.send_message("🚀 加密货币监控系统已启动")
        # 通知类（企业微信）
        self.wechat_notifier = EnterpriseWeChatNotifier(config.get("wechat_webhook", ""))
        if config.get("enable_wechat"):
            self.wechat_notifier.enabled = True
            self.wechat_notifier.send_message("🚀 加密货币监控系统已启动（企业微信通知）")


        # 初始化交易所
        Thread(target=self.init_exchanges_with_retry, daemon=True).start()

        # 启动内存优化线程
        self.optimizer = MemoryOptimizer(self, config.get('mem_interval', 600))
        self.optimizer.start()

    def log(self, message: str, category: str = 'log') -> None:
        """统一日志记录，同时回调给GUI显示"""
        if self.log_callback:
            print("DEBUG: log_callback type:", type(self.log_callback))
            self.log_callback(message, category)

    def get_markets(self, exchange_id: str) -> Dict:
        """
        获取并缓存交易所市场数据，缓存时效为300秒
        同时更新USDT交易对缓存
        """
        with self.data_lock:
            cache = self.market_cache.get(exchange_id)
            if cache and (datetime.now() - cache['timestamp']).seconds < 300:
                return cache['markets']

            exchange = self.exchanges[exchange_id]
            try:
                markets = exchange.load_markets()
                self.market_cache[exchange_id] = {
                    'timestamp': datetime.now(),
                    'markets': markets
                }
                self.usdt_pairs_cache[exchange_id] = [
                    clean_symbol(s) for s in markets
                    if ((s.endswith('/USDT') or s.endswith('-USDT') or 'usdt' in s.lower())
                    and markets[s]['active'])
                ]
                self.log(f"✅ 交易所 {exchange_id} 可用交易对: {self.usdt_pairs_cache[exchange_id]}", "log")

                return markets
            except Exception as e:
                self.log(f"市场数据加载失败({exchange_id}): {str(e)}", 'warning')
                return {}
        
    #==============

    def _calculate_moving_averages(self, closes: pd.Series, periods: List[int]) -> Dict[int, pd.Series]:
        """
        计算指定周期的移动平均，支持TA-Lib或Pandas rolling
        """
        ma_values = {}
        for period in periods:
            if TA_LIB_AVAILABLE:
                ma_values[period] = talib.SMA(closes, period)
            else:
                # 仅在第一次输出日志提示
                if not hasattr(self, '_talib_unavailable_logged'):
                    self.log("TA-Lib不可用，使用普通移动平均计算", 'warning')
                    self._talib_unavailable_logged = True
                ma_values[period] = closes.rolling(period).mean()
        return ma_values
    

    def safe_fetch_ohlcv(self, exchange: ccxt.Exchange, symbol: str, timeframe: str, limit: int) -> Optional[list]:
        """
        获取K线数据，确保数据格式正确，并防止异常数据存入 `price_data`
        """
        if not self.init_complete.is_set():
            return None

        # 针对HTX的额外延迟
        if exchange.id == 'htx':
            time.sleep(0.25)  # 控制HTX请求间隔至少250ms

        #for _ in range(3):
        for attempt in range(3):
            try:
                time.sleep(max(exchange.rateLimit / 1000, 0.1))
                data = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
                # 强化数据验证
                if not data or len(data) < limit:
                    self.log(f"⚠️ {symbol} 数据不足，需要 {limit} 条，实际获取 {len(data)} 条", "warning")
                    continue  # 继续重试

                # ✅ 确保 `data` 是 `list[list]`
                if not isinstance(data, list) or not all(isinstance(i, list) and len(i) > 4 for i in data):
                    self.log(f"⚠️ {symbol} 返回数据异常: {data}", "warning")
                    return None

                latest_candle = data[-1]  # 取最新一根K线
                latest_close = float(latest_candle[4]) if latest_candle[4] is not None else None
                if len(latest_candle) < 5 or latest_candle[4] is None:
                    self.log(f"⚠️ {symbol} K线格式异常", "warning")
                    continue
                if latest_close is None:
                    self.log(f"⚠️ {symbol} 最新K线收盘价为 None: {latest_candle}", "warning")
                    return None

                with self.stats_lock:
                    self.connection_stats['total_pairs'] += 1
                    self.connection_stats['success_pairs'] += 1
                    self.connection_stats['last_update'] = datetime.now()

                with self.data_lock:
                    # **确保 `self.base_prices[symbol]` 只存 float**
                    if symbol not in self.base_prices:
                        self.base_prices[symbol] = latest_close

                    # **确保 `self.price_data[symbol]` 只存 `dict`**
                    self.price_data[symbol] = {
                        'timestamp': time.time(),
                        'data': latest_close  # 确保 `float` 类型
                    }
                    self.log(f"✅ 存储 {symbol} 价格: {self.price_data[symbol]}", "log")

                return data  # ✅ 返回完整 K 线数据

            except ccxt.NetworkError as e:
                self.log(f"网络错误: {str(e)} - {symbol}", 'warning')
                time.sleep(10)
            except ccxt.ExchangeError as e:
                self.log(f"交易所错误: {str(e)} - {symbol}", 'warning')
            except Exception as e:
                #self.log(f"未知错误: {str(e)} - {symbol}", 'warning')
                self.log(f"第 {attempt+1} 次获取 {symbol} 数据失败: {str(e)}", "warning")
                time.sleep(2)


        # 三次尝试均失败后记录
        self.log(f"❌ {symbol} 数据获取彻底失败", "warning")
        return None  # ✅ 确保函数返回 `None`

    
    
    def get_leaderboard(self, top_n: int = 10) -> Dict[str, Any]:
        """
        获取涨幅排行榜 + 监控统计信息
        top_n: 返回排行榜的数量（默认前10名）
        """
        leaderboard = []
        with self.data_lock:
            for symbol, info in self.price_data.items():
                    current_price = info.get('data')
                    base_price = self.base_prices.get(symbol)
                
                    # 确保数据有效（基准价格不能为0）
                    if base_price and base_price > 0:
                        change = (current_price - base_price) / base_price * 100
                        leaderboard.append({
                            "symbol": symbol,
                            "exchange": "N/A",  # 如果需要可进一步完善此处的交易所信息
                            "current_price": current_price,
                            "base_price": base_price,
                            "change": change
                        })

        # 按涨幅降序排序，剔除异常数据
        leaderboard = sorted(leaderboard, key=lambda x: x["change"], reverse=True)[:top_n]

        return {
            "leaderboard": leaderboard,
            "monitor_stats": self.history.get_stats()  # 添加监控统计信息
        }


    def cleanup_data(self) -> None:
        """
        定期清理过期数据：
          - 价格数据保留2小时
          - 均线数据保留24小时
          - 交易所市场缓存保留1小时
          - 清空USDT交易对缓存（由下次调用get_markets重新填充）
        """
        now_time = time.time()
        with self.data_lock:
            self.price_data = {
                k: v for k, v in self.price_data.items()
                if now_time - v['timestamp'] < 7200
            }
            self.log(f"💾 内存清理后仍存储的交易对: {list(self.price_data.keys())}", "log")

            self.ma_data = {
                k: v for k, v in self.ma_data.items()
                if now_time - v['timestamp'] < 86400
            }
            # 清理交易所市场缓存
            for ex_id in list(self.market_cache.keys()):
                if (datetime.now() - self.market_cache[ex_id]['timestamp']).seconds > 3600:
                    del self.market_cache[ex_id]
            # 清空USDT缓存，等待下次刷新
            self.usdt_pairs_cache.clear()

    def init_exchanges_with_retry(self) -> None:
        """
        并行初始化所选交易所，重试三次后标记为“disconnected”
        """
        self.log("初始化交易所连接中...", "log")
        # 新增代理测试逻辑
        proxy = self.config['proxy']
        if proxy:
            try:
                test_url = "https://api.binance.com/api/v3/ping"  # 使用通用测试地址
                response = requests.get(test_url, 
                                      proxies={'http': proxy, 'https': proxy}, 
                                      timeout=10)
                if response.status_code != 200:
                    self.log(f"代理 {proxy} 测试失败: HTTP {response.status_code}", 'warning')
                    return
            except Exception as e:
                self.log(f"代理 {proxy} 不可用: {str(e)}", 'warning')
                # return # 不 return，继续尝试连接交易所
         # 原有初始化逻辑（增加HTX的headers）   
        def init_single_exchange(exchange_id: str):
            self.exchange_status[exchange_id] = 'connecting'
            for attempt in range(3):
                try:
                    self.log(f"正在连接 {exchange_id} ({attempt+1}/3)...")
                    exchange_class = getattr(ccxt, exchange_id)
                    exchange = exchange_class({
                        'proxies': {'http': self.config['proxy'], 'https': self.config['proxy']},
                        'enableRateLimit': True,
                            'headers': {
                                       'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                                       'Referer': 'https://www.htx.com/'  # 添加Referer绕过反爬
                    },
                        'verify': False  # 注意：此处禁用了 SSL 验证，仅用于调试，生产环境下不建议关闭验证！
                    })
                    exchange.load_markets()
                    with self.data_lock:
                        self.exchanges[exchange_id] = exchange
                        self.exchange_status[exchange_id] = 'connected'
                    self.log(f"{exchange_id} 连接成功", 'log')
                    return
                except ccxt.DDoSProtection as e:
                    self.log(f"交易所 {exchange_id} 触发DDoS保护: {str(e)}", 'warning')
                except ccxt.ExchangeNotAvailable as e:
                    self.log(f"交易所 {exchange_id} 暂时不可用: {str(e)}", 'warning')
                except Exception as e:
                    self.log(f"{exchange_id} 连接失败: {str(e)}", 'warning')
                time.sleep(10)
            self.exchange_status[exchange_id] = 'disconnected'

        threads = []
        for exchange_id in self.config['exchanges']:
            t = Thread(target=init_single_exchange, args=(exchange_id,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        self.init_complete.set()

    def send_alert(self, exchange_id: str, symbol: str, message: str, alert_type: str) -> None:
        """
        发送警报：
          - 格式化警报消息，记录到历史记录
          - 通过Telegram发送（如果启用）
          - 通过日志回调显示在GUI上
          - 新增监控统计数据（新增排行榜数据）
        """
        key = (symbol, alert_type)
        current_time = time.time()
        # 如果该警报曾经发送过，并且距离上次发送时间小于冷却时间，则不再发送
        if key in self.last_alert_time and (current_time - self.last_alert_time[key] < self.alert_cooldown):
            return  # 冷却中，不发送

        # 更新最后发送时间
        self.last_alert_time[key] = current_time

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        full_message = f"[{timestamp}] {exchange_id.upper()} {symbol} - {message}"

            # 在记录中保存实际使用的参数
        record = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'exchange': exchange_id,
            'symbol': symbol,
            'message': message,
            'type': alert_type,
            'timeframe': self.config.get('price_timeframe', '1m'),  # 记录实际参数
            'period': self.config.get('price_period', 15)
        }
        self.history.add_record(record)

        # 记录到历史记录
        #self.history.add_record({
            #'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            #'exchange': exchange_id,
            #'symbol': symbol,
           # 'message': message,
           # 'type': alert_type,
          #  'timeframe': self.config.get('price_timeframe', '1m'),  # 记录实际参数
          #  'period': self.config.get('price_period', 15)
       # })

        # 获取监控统计 & 排行榜
        leaderboard_data = self.get_leaderboard()
        stats = leaderboard_data["monitor_stats"]

        # 📊 监控统计信息
        stats_message = f"\n📊 监控统计:\n🔼 多头: {stats['bullish_signals']} 🔽 空头: {stats['bearish_signals']}"

        # 🏆 涨幅排行榜
        leaderboard_message = "\n🏆 涨幅榜:"
        for i, entry in enumerate(leaderboard_data["leaderboard"], 1):
            leaderboard_message += f"\n{i}️⃣ {entry['symbol']} ({entry['exchange']}) {entry['change']:.2f}%"

        final_message = full_message + stats_message + leaderboard_message

        # 发送 Telegram
        if self.notifier.enabled:
            self.notifier.send_message(final_message)

        # 发送企业微信
        if self.wechat_notifier and self.wechat_notifier.enabled:
            self.wechat_notifier.send_message(final_message)


    def check_price_alert(self, exchange: ccxt.Exchange, symbol: str,config: dict) -> None:
        """
        检查虚拟货币价格：
        - 计算自定义周期（price_period）内的涨跌幅
        - 如果达到设定的阈值（price_threshold），则触发提醒
        """
        if not self.config.get('enable_price_monitor', False):
            return
        # 从配置中动态读取参数

        price_timeframe = self.config.get('price_timeframe', "1m")
        price_period = self.config.get('price_period', 15)
        threshold = config.get('price_threshold', 5.0)
        direction = config.get('price_direction', 'both').lower()

        data = self.safe_fetch_ohlcv(exchange, symbol, price_timeframe, limit=price_period)

        # ✅ 确保数据格式正确
        if not isinstance(data, list) or len(data) < price_period:
            self.log(f"⚠️ {symbol} 数据不足 ({len(data) if isinstance(data, list) else 'None'})，无法计算涨跌幅", "warning")
            return

        try:
            # ✅ 取第一根和最后一根K线数据
            first_candle = data[0] if isinstance(data[0], list) and len(data[0]) > 4 else None
            last_candle = data[-1] if isinstance(data[-1], list) and len(data[-1]) > 4 else None

            if first_candle is None or last_candle is None:
                self.log(f"⚠️ {symbol} K线数据格式异常: first_candle={first_candle}, last_candle={last_candle}", "warning")
                return

            past_close = float(first_candle[4]) if first_candle[4] is not None else None
            current_close = float(last_candle[4]) if last_candle[4] is not None else None

            if past_close is None or current_close is None or past_close <= 0:
                self.log(f"⚠️ {symbol} 价格异常: past_close={past_close}, current_close={current_close}", "warning")
                return

            # ✅ 计算涨跌幅
            change_percent = ((current_close - past_close) / past_close) * 100
            threshold = self.config.get('price_threshold', 5.0)
            if not isinstance(threshold, (int, float)) or threshold <= 0:
                self.log(f"⚠️ 无效的价格阈值: {threshold}", "warning")
                return

            # ✅ 方向判断，确保值合法
            direction = self.config.get('price_direction', 'both').lower()
            alert_triggered = False
            alert_msg = ""

            if direction in ['up', 'both'] and change_percent >= threshold:
                alert_msg = f"📈 价格上涨 {change_percent:.2f}%（{price_period}分钟）"
                alert_triggered = True
            elif direction in ['down', 'both'] and change_percent <= -threshold:
                alert_msg = f"📉 价格下跌 {abs(change_percent):.2f}%（{price_period}分钟）"
                alert_triggered = True

            # ✅ 发送警报
            if alert_triggered:
                self.send_alert(exchange.id, symbol, alert_msg, 'price')

        except Exception as e:
            self.log(f"⚠️ {symbol} 价格计算异常: {str(e)}", "warning")
    


    def check_ma_alerts(self, exchange: ccxt.Exchange, symbol: str) -> List[Tuple[str, str]]:
        """
        检测均线策略信号：
          - 遍历每组均线策略（周期、时间周期可配置）
          - 使用K线数据计算移动均线
          - 判断短/中/长期均线的排列与中轨价格偏离，确认多头或空头信号
        返回 [(alert_type, message), ...] 的列表
        """
        alerts = []
        for strategy in self.config.get('ma_strategies', []):
            periods = strategy.get('periods')
            timeframe = strategy.get('timeframe')
            required_length = max(periods) + 10
            data = self.safe_fetch_ohlcv(exchange, symbol, timeframe, limit=required_length)
            if not data or len(data) < required_length:
                continue
            closes = pd.Series([candle[4] for candle in data])
            current_price = closes.iloc[-1]
            ma_values = self._calculate_moving_averages(closes, periods)
            ma_short, ma_medium, ma_long = periods
            ma_short_val = ma_values[ma_short].iloc[-1]
            ma_medium_val = ma_values[ma_medium].iloc[-1]
            ma_long_val = ma_values[ma_long].iloc[-1]

            # 检查中轨偏离：1%容差
            if abs(current_price - ma_medium_val) / ma_medium_val > 0.01:
                continue

            # 判断多头信号：短 > 中 > 长，且最近10根K线均高于中轨
            #if (ma_short_val > ma_medium_val > ma_long_val and
               # all(closes.iloc[i] > ma_medium_val for i in range(-10, 0))):
               # alerts.append(("bullish", f"{symbol} 均线多头排列"))
            # 判断空头信号：短 < 中 < 长，且最近10根K线均低于中轨
            #elif (ma_short_val < ma_medium_val < ma_long_val and
                 # all(closes.iloc[i] < ma_medium_val for i in range(-10, 0))):
               # alerts.append(("bearish", f"{symbol} 均线空头排列"))
            
            # 检测多头排列：启用多头策略时，短期 > 中期 > 长期，且最近10根K线的收盘价均高于中期均线
            if self.config.get("enable_bullish_ma", False) and \
               ma_short_val > ma_medium_val > ma_long_val and \
               all(closes.iloc[i] > ma_medium_val for i in range(-10, 0)):
                alerts.append(("bullish", f"{symbol} 形成多头排列"))

            # 检测空头排列：启用空头策略时，短期 < 中期 < 长期，且最近10根K线的收盘价均低于中期均线
            if self.config.get("enable_bearish_ma", False) and \
               ma_short_val < ma_medium_val < ma_long_val and \
               all(closes.iloc[i] < ma_medium_val for i in range(-10, 0)):
                alerts.append(("bearish", f"{symbol} 形成空头排列"))
        return alerts

    def monitor_single_pair(self, exchange: ccxt.Exchange, symbol: str, alert_counts: dict) -> None:
        """
        监控单个交易对：
          - 检测价格警报
          - 检测均线策略警报
        """
        retry_count = 0
        max_retries = 3
        while retry_count < max_retries and self.running.is_set():
            try:
                if not self.running.is_set():
                    return
    
                self.log(f"⏳ 监控 {symbol}...", "log")

                # ✅ 确保 `self.price_data[symbol]` 是 `dict`
                price_info = self.price_data.get(symbol)
                if not price_info or not isinstance(price_info, dict) or 'data' not in price_info:
                    self.log(f"⚠️ {symbol} 第 {retry_count+1} 次重试获取价格数据...", "warning")
                    # 尝试重新调用 safe_fetch_ohlcv() 以更新价格数据
                    timeframe = self.config.get('price_timeframe', "1m")
                    period = self.config.get('price_period', 15)
                    data = self.safe_fetch_ohlcv(exchange, symbol, timeframe, limit=period)
                    if not data:
                        retry_count += 1
                        time.sleep(2)
                        self.log(f"⚠️ {symbol} 重新获取价格数据失败", "warning")
                        return
                    # 重新检查更新后的价格数据
                    price_info = self.price_data.get(symbol)
                    if not price_info or 'data' not in price_info:
                        raise ValueError("价格数据刷新失败")
                        self.log(f"⚠️ {symbol} 重新获取后仍无有效价格数据", "warning")
                        return

                # ✅ 价格警报检测
                self.check_price_alert(exchange, symbol)

                # ✅ 均线策略检测
                ma_alerts = self.check_ma_alerts(exchange, symbol) or []
                if not isinstance(ma_alerts, list):
                    self.log(f"⚠️ {symbol} 均线策略返回异常: {ma_alerts}", "warning")
                    return

                for alert_type, msg in ma_alerts:
                    self.send_alert(exchange.id, symbol, msg, alert_type)
                    alert_counts[alert_type] = alert_counts.get(alert_type, 0) + 1

                time.sleep(0.5)  # 控制请求频率
                break  # 成功则退出循环

            except Exception as e:
                retry_count += 1
                self.log(f"⚠️ {symbol} 监控异常（{retry_count}/{max_retries}）: {str(e)}", "warning")
                time.sleep(3)

    def start_monitoring(self, symbol) -> None:
        """
        主监控循环：
          - 遍历每个交易所，过滤USDT交易对（支持排除及最大数量限制）
          - 使用线程池并行检测每个交易对的监控信号
          - 定时更新连接统计与日志输出
        """
        self.log("监控循环启动", 'log')
        while self.running.is_set():
            # 每次循环前动态读取最新配置
            with self.data_lock:  # 加锁保证线程安全
                current_config = self.config.copy()
            # 使用 current_config 进行检测
            self.check_price_alert(exchange, symbol, current_config)  # 传递当前配置
            try:
                # ✅ 先读取当前统计数据，避免清零影响 `get_leaderboard()`
                previous_stats = self.history.get_stats()  # 先获取之前的统计数据

                # ✅ 清零统计，防止数据累积
                self.history.stats = {"bullish_signals": 0, "bearish_signals": 0}

                alert_counts = {'bullish': 0, 'bearish': 0}
                for exchange_id, exchange in self.exchanges.items():
                    if self.exchange_status.get(exchange_id) != 'connected':
                        continue
                
                    # ✅ 确保 `self.get_markets(exchange_id)` 不返回 `None`
                    markets = self.get_markets(exchange_id) or {}

                    # ✅ 确保 `usdt_pairs_cache` 可用
                    usdt_pairs = self.usdt_pairs_cache.get(exchange_id, [])
                    if not usdt_pairs:
                        usdt_pairs = [s for s in markets if s.endswith('/USDT')]

                    # ✅ 排除指定交易对
                    excluded = [x.strip().upper() for x in self.config.get('excluded_pairs', '').split(',') if x]
                    monitored_pairs = [p for p in usdt_pairs if p.upper() not in excluded][:self.config.get('max_pairs', 500)]

                    # ✅ 动态分配 `max_workers`，防止线程浪费
                    max_threads = min(10, len(monitored_pairs) // 2 + 1)
    
                    # ✅ 并行处理交易对
                    with ThreadPoolExecutor(max_workers=max_threads) as executor:
                        futures = [executor.submit(self.monitor_single_pair, exchange, symbol, alert_counts) for symbol in monitored_pairs]
                        for future in as_completed(futures):
                            pass

                # 📊 监控统计 & 排行榜
                leaderboard_data = self.get_leaderboard() or {"leaderboard": []}  # ✅ 确保 `leaderboard_data` 可用

                stats_msg = (f"📊 监控统计 | 🔼 多头: {alert_counts.get('bullish', 0)} "
                             f"🔽 空头: {alert_counts.get('bearish', 0)}")

                leaderboard_msg = "\n🏆 涨幅榜:"
                for i, entry in enumerate(leaderboard_data["leaderboard"], 1):
                    leaderboard_msg += f"\n{i}️⃣ {entry.get('symbol', '未知')} ({entry.get('exchange', '未知')}) {entry.get('change', 0.0):.2f}%"

                full_message = stats_msg + leaderboard_msg

                # ✅ 发送 Telegram & 企业微信
                if self.notifier.enabled:
                    self.notifier.send_message(full_message)

                if self.wechat_notifier and self.wechat_notifier.enabled:
                    self.wechat_notifier.send_message(full_message)

                # ✅ 防止 `check_interval` 为 `None`
                #time.sleep(self.config.get('check_interval', 300) or 300)
                # 动态调整休眠时间
                time.sleep(current_config.get('check_interval', 300))
        
            except Exception as e:
                self.log(f"⚠️ 监控循环异常: {str(e)}", 'warning')
                time.sleep(30)  # ✅ 避免短时间内死循环

    # 新增接口：更新整个监控配置
    def update_config(self, new_config: dict) -> None:
        """
        更新监控对象内部的配置字典，新的配置会在下一次监控周期生效。
        """
        with self.data_lock:  # 加锁
            changed_keys = [k for k in new_config if self.config.get(k) != new_config[k]]
            # 记录需要重新初始化的字段
            need_reinit = 'price_timeframe' in new_config
        # 合并配置
            self.config.update(new_config)
        

            # 特殊字段处理
            if need_reinit:
                self._reinit_data_fetcher()
    
        self.log(f"配置已实时更新: {new_config}", "log")
        self.log(f"配置变更字段: {changed_keys}", "debug")

    def _reinit_data_fetcher(self):
        """重新初始化数据抓取器（真实实现）"""
        # 清空旧数据缓存
        with self.data_lock:
            self.price_data.clear()
            self.base_prices.clear()
            
        self.log("价格时间周期变更，已清空缓存数据", "log")

    # 新增接口：更新单个交易对监控列表
    def update_single_pair_list(self, pair_list: list) -> None:
        """
        更新单个交易对监控列表，重置内部的单对策略配置（可根据需要保留原有策略）。
        这里简单将原有策略清空，并以新列表初始化每个交易对为“未启用”状态。
        """
        self.single_pair_strategies = {}
        for pair in pair_list:
            self.single_pair_strategies[pair.upper()] = {"enabled": False}
        self.log("单个交易对监控列表已更新", "log")
    #=====================================================
    def enable_single_pair_strategy(self, pair: str, strategy1: dict, strategy2: dict) -> None:
        """
        启用单个交易对监控：
          - pair: 交易对代码，如 "BTC/USDT"
          - strategy1: 字典，包含策略1参数，例如：
              {"timeframe": "5m", "ma_period": 20, "threshold": 5}
          - strategy2: 字典，包含策略2参数，例如：
              {"timeframe": "5m", "threshold": 10}
        此方法保存配置，并启动后台线程进行监控。
        """
        pair = pair.upper()
        if not hasattr(self, "single_pair_strategies"):
            self.single_pair_strategies = {}
        self.single_pair_strategies[pair] = {
            "strategy1": strategy1,
            "strategy2": strategy2,
            "enabled": True
        }
        self.log(f"单个交易对 {pair} 监控已启用", "log")
        # 启动监控线程，如果尚未启动该交易对的监控线程（你可以简单启动一个新的线程，每次启用均启动）
        thread = Thread(target=self.monitor_single_pair_strategy, args=(pair,), daemon=True)
        thread.start()

    def disable_single_pair_strategy(self, pair: str) -> None:
        """
        禁用单个交易对监控，将对应策略标记为禁用。
        """
        pair = pair.upper()
        if hasattr(self, "single_pair_strategies") and pair in self.single_pair_strategies:
            self.single_pair_strategies[pair]["enabled"] = False
            self.log(f"单个交易对 {pair} 监控已禁用", "log")
        else:
            self.log(f"未找到 {pair} 的单对监控策略", "warning")

    def monitor_single_pair_strategy(self, pair: str) -> None:
        """
        后台线程循环监控指定交易对，分别应用策略1和策略2，
        当达到警报条件时调用 send_alert 触发报警。
        这里示例中使用默认刷新间隔为 60 秒，你可根据需要调整或将其作为参数设置。
        """
        refresh_interval = 60  # 单对监控刷新间隔（秒）
        while (hasattr(self, "single_pair_strategies") and
               pair in self.single_pair_strategies and
               self.single_pair_strategies[pair].get("enabled", False) and
               self.running.is_set()):
            # 取出策略配置
            strat = self.single_pair_strategies[pair]
            s1 = strat["strategy1"]
            s2 = strat["strategy2"]

            # 选择一个交易所进行监控，这里示例使用 Binance（你可根据实际情况选择其他交易所）
            exchange = self.exchanges.get("binance")
            if not exchange:
                self.log(f"监控 {pair} 时未连接 Binance", "warning")
                time.sleep(refresh_interval)
                continue

            # 策略1：监控价格与均线偏离
            data1 = self.safe_fetch_ohlcv(exchange, pair, s1.get("timeframe", "5m"), limit=s1.get("ma_period", 20) + 10)
            if data1 and len(data1) >= s1.get("ma_period", 20):
                closes = pd.Series([candle[4] for candle in data1])
                ma_line = closes.rolling(window=s1.get("ma_period", 20)).mean()
                current_price = closes.iloc[-1]
                current_ma = ma_line.iloc[-1]
                if current_ma and current_ma != 0:
                    diff_pct = abs(current_price - current_ma) / current_ma * 100
                    if diff_pct <= s1.get("threshold", 5):
                        self.send_alert(exchange.id, pair, 
                                        f"策略1：价格 {current_price:.2f} 与均线 {current_ma:.2f} 差 {diff_pct:.2f}%", 
                                        "strategy1")
            
            # 策略2：监控价格涨跌幅百分比
            data2 = self.safe_fetch_ohlcv(exchange, pair, s2.get("timeframe", "5m"), limit=2)
            if data2 and len(data2) >= 2:
                prev_close = data2[-2][4]
                current_close = data2[-1][4]
                if prev_close != 0:
                    change_pct = ((current_close - prev_close) / prev_close) * 100
                    if abs(change_pct) >= s2.get("threshold", 10):
                        self.send_alert(exchange.id, pair, 
                                        f"策略2：价格变动 {change_pct:.2f}%", 
                                        "strategy2")
            time.sleep(refresh_interval)

# ======================== 股票类 ========================

class StockMonitorPro:
    def __init__(self, config, log_callback=None):
        """
        股票监控类，延迟导入 Akshare，避免影响 GUI 启动。
        config 示例：
          {
            'stock_list': ['sh600519', 'sz000858'],  # 股票代码（Akshare 格式，如 'sh600519'）
            'stock_period': 'daily',                # 可选："daily", "weekly", "5min" 等
            'stock_check_interval': 60,             # 检查间隔（秒）
            'ma_period': 30,                        # 均线周期，比如 30 日均线或 30 分钟均线
            'ma_threshold': 2.0                     # 当股价与均线差距在 2% 内触发报警
          }
        """
        from threading import Event
        self.config = config
        self.log_callback = log_callback
        self.running = Event()
        self.running.set()  # 标记为运行状态
        # 新增：数据缓存，用于预加载股票数据
        self.data_cache = {}
        self.allowed_periods = ["5min", "15min", "30min", "60min", "120min", "240min", "daily"]


    def update_config(self, new_config: dict) -> None:
        """
        更新股票监控对象的配置
        """
        self.config.update(new_config)
        self.log("股票监控配置已更新", "log")
        # 如果你希望单独更新监控的股票代码列表，可以增加如下方法：
    def update_stock_list(self, stock_list: list) -> None:
        """
        更新股票监控的股票代码列表。
        """
        self.config['stock_list'] = ",".join(stock_list)
        self.log("股票监控股票列表已更新", "log")

    def fetch_stock_data(self, symbol, period, count=200):
        """获取不同周期的股票数据"""
        period = self.config.get("stock_period", "5min")
        if period not in self.allowed_periods:
            self.log(f"不支持的时间周期: {period}", "warning")
            return None
        
        return fetch_stock_data(symbol, period=period, count=200)
    
    def log(self, message, category='log'):
        if self.log_callback:
            self.log_callback(message, category)
        else:
            print(f"[{category}] {message}")
    
    def calculate_ma(self, closes: pd.Series):
        period = self.config.get('ma_period', 30)
        return closes.rolling(window=period, min_periods=1).mean()  # 允许最小周期为1，避免初期数据不足

    def check_stock(self, symbol, df=None):
        if df is None:
            df = self.data_cache.get(symbol)
            if df is None or df.empty:
                df = self.fetch_stock_data(symbol)
                if df is None or df.empty:
                    return

        closes = df["收盘"]
        ma_period = self.config.get("ma_period", 30)
        ma_line = closes.rolling(window=ma_period).mean()
        current_price = closes.iloc[-1]
        current_ma = ma_line.iloc[-1]
        if pd.isna(current_ma) or current_ma == 0:
            return
        diff_pct = abs(current_price - current_ma) / current_ma * 100
        if diff_pct <= self.config.get('ma_threshold', 2.0):
            self.log(f"⚠️ 股票 {symbol}: 当前价格 {current_price:.2f} 与 {self.config.get('ma_period', 30)}期均线 {current_ma:.2f} 差距 {diff_pct:.2f}%", "stock")


    def start_monitoring(self) -> None:
        """
        股票监控循环：
        监控指定股票的最新价格是否在自定义均线的百分比阈值范围内，触发提醒。
        不判断多头或空头，只要价格偏离均线小于等于设定的百分比就提醒。

        监控周期、均线周期、百分比阈值均可在 GUI 中自定义，修改后下一轮监控生效。
        """
        self.log("📈 股票监控循环启动...", "log")
    
        while self.running.is_set():
            try:
                # 获取配置中的股票列表
                stock_list = self.config.get('stock_list', [])
                if isinstance(stock_list, str):
                    stock_list = [s.strip() for s in stock_list.split(',') if s.strip()]

                # 遍历股票列表
                for symbol in stock_list:
                    self.log(f"⏳ 获取 {symbol} 的数据...", "log")

                    # ✅ 在这里获取每只股票的数据
                    df = self.fetch_stock_data(symbol, period=self.config.get("stock_period", "5min"), count=200)

                    if df is not None and not df.empty:
                        print(f"{symbol} 数据获取成功:\n{df.head()}")  # ✅ 打印前几行数据检查格式
                        self.log(f"✅ {symbol} 数据获取成功，共 {len(df)} 条 K 线数据", "log")

                        # 进行均线监控
                        self.check_stock(symbol, df)
                    else:
                        self.log(f"⚠️ {symbol} 数据获取失败，可能已退市或数据源错误", "warning")

                # 设定下一次检查的时间间隔
                check_interval = self.config.get("stock_check_interval", 60)
                self.log(f"⏳ 等待 {check_interval} 秒后进入下一轮监控", "log")
                time.sleep(check_interval)

            except Exception as e:
                self.log(f"❌ 股票监控循环异常: {e}", "warning")
                time.sleep(30)

# ======================== GUI界面 ========================
class MonitorGUIPro(tk.Tk):
    """
    图形界面：
      - 包含交易所选择、代理与Telegram配置
      - 提供策略参数配置（价格监控、均线策略、多组策略支持）
      - 实时显示状态、日志、警报队列
      - 支持启动、停止、紧急停止与配置保存
    """

    def __init__(self):
        super().__init__()
        self.title("专业加密货币监控系统 v7.3")
        self.geometry("1200x900")
        #self.monitor = None
        self.stock_monitor = None  # 新增股票监控对象
        self._pending_alerts = deque(maxlen=100)
        self._setup_ui()
       # self.after(5000, self.update_leaderboard)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.load_config()
        self.is_monitoring = False  # 添加一个标志变量用于控制监控进程是否启动
            # 允许窗口灵活缩放
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        # ✅ 确保 monitor 被初始化
        config = self.load_config()
        if config is None:
            print("⚠️ 配置加载失败，使用默认配置！")
            config = {}  # 避免 None 传递导致错误

        self.monitor = CryptoMonitorPro(config, log_callback=self.log_message)
        # ✅ 确保 leaderboard_listbox 正确初始化
        self.leaderboard_listbox = tk.Listbox(self)
        self.leaderboard_listbox.pack(pady=10, fill=tk.BOTH, expand=True)
        # 启动排行榜自动刷新
        self.after(5000, self.update_leaderboard)
    
    #===========================================
    def _create_status_light(self, text: str, parent) -> tk.Label:
        """
        在指定父容器 parent 中创建一个状态指示灯控件，
        显示传入的 text 和一个圆点（初始为灰色）。
        """
        frame = ttk.Frame(parent)
        frame.pack(side=tk.LEFT, padx=5)
        ttk.Label(frame, text=text).pack(side=tk.LEFT)
        indicator = tk.Label(frame, text="●", font=('Arial', 12), fg="gray")
        indicator.pack(side=tk.LEFT)
        return indicator


    def _setup_ui(self) -> None:
        """初始化所有界面组件"""

        # 创建 Notebook 控件（两个标签页：加密货币和股票）
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(expand=True, fill=tk.BOTH)

        # 加密货币页面
        self.crypto_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.crypto_frame, text="加密货币")
        self._create_crypto_page(self.crypto_frame)

        # 股票监控页面
        self.stock_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.stock_frame, text="股票")
        self._create_stock_page(self.stock_frame)

        # 在 Notebook 下方添加共享日志与排行榜区域
        self._create_log_and_leaderboard_panel()

        self.after(1000, self.update_status)
        self.after(500, self.process_alerts)
        self.bind("<<LogUpdate>>", lambda e: self.process_alerts())  # 新增事件绑定


    #==========================================

    def _create_crypto_page(self, parent):
        """
        在传入的父容器 parent 中创建加密货币监控页面，
        包括：交易所选择、状态指示、网络及通知配置、操作按钮等。
        """
        # 控制面板整体区域
        control_frame = ttk.Frame(parent)
        control_frame.pack(pady=5, fill=tk.X, padx=5)
        # 第一行：交易所选择与状态指示灯
        row1 = ttk.Frame(control_frame)
        row1.pack(fill=tk.X)

        # 初始化状态标签字典
        self.exchange_status_labels = {}
        # 币安
        ttk.Label(row1, text="交易所:").grid(row=0, column=0, padx=5, sticky=tk.W)
        self.binance_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row1, text="BINANCE", variable=self.binance_var).grid(row=0, column=1, padx=2)
        ttk.Button(row1, text="检测", command=lambda: self.test_exchange("BINANCE", "https://api.binance.com/api/v3/ping"), width=5).grid(row=0, column=2, padx=2)
    
        # 欧易（OKX）
        self.okx_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="OKX", variable=self.okx_var).grid(row=0, column=3, padx=2)
        ttk.Button(row1, text="检测", command=lambda: self.test_exchange("OKX", "https://www.okx.com/api/v5/public/instruments?instType=SPOT"), width=5).grid(row=0, column=4, padx=2)
    
        # 火币（假设使用正确的 ccxt 标识，此处显示为“火币”）
        self.htx_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row1, text="HTX", variable=self.htx_var).grid(row=0, column=5, padx=2)
        ttk.Button(row1, text="检测", command=lambda: self.test_exchange("HTX", "https://api.htx.com/v1/common/symbols"), width=5).grid(row=0, column=6, padx=2)
    
        # 状态指示灯区域（可放在同一行）
        self.status_frame = ttk.Frame(row1)
        self.status_frame.grid(row=0, column=7, padx=10)
        ttk.Label(self.status_frame, text="状态:").pack(side=tk.LEFT)
        self.binance_indicator = self._create_status_light('币安')
        self.okx_indicator = self._create_status_light('欧易')
        self.htx_indicator = self._create_status_light('火币')

        # 第二行：网络、通知及操作按钮（包含新增的企业微信设置）
        row2 = ttk.Frame(control_frame)
        row2.pack(fill=tk.X, pady=3)

        # 添加代理开关与输入框
        self.proxy_enable = tk.BooleanVar(value=True)
        ttk.Checkbutton(row2, text="启用代理", variable=self.proxy_enable, command=self.toggle_proxy_entry).grid(row=0, column=0, padx=5, sticky=tk.W)
        self.proxy_entry = ttk.Entry(row2, width=25, state=tk.NORMAL)
        self.proxy_entry.insert(0, "http://127.0.0.1:10809")
        self.proxy_entry.grid(row=0, column=1, padx=2)
        ttk.Button(row2, text="测试代理", command=self.test_proxy, width=5).grid(row=0, column=2, padx=2)
        # Telegram设置
        ttk.Label(row2, text="TG Token:").grid(row=0, column=3, padx=2, sticky=tk.W)
        self.tg_token_entry = ttk.Entry(row2, width=30)
        self.tg_token_entry.grid(row=0, column=4, padx=2)
        ttk.Label(row2, text="Chat ID:").grid(row=0, column=5, padx=2, sticky=tk.W)
        self.tg_chat_entry = ttk.Entry(row2, width=15)
        self.tg_chat_entry.grid(row=0, column=6, padx=2)
        self.tg_enable = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="启用TG", variable=self.tg_enable).grid(row=0, column=7, padx=2)
        ttk.Button(row2, text="测试TG", command=self.test_telegram, width=5).grid(row=0, column=8, padx=2)
        # 企业微信设置
        ttk.Label(row2, text="企业微信 Webhook:").grid(row=1, column=0, padx=2, sticky=tk.W)
        self.wechat_webhook_entry = ttk.Entry(row2, width=40)
        self.wechat_webhook_entry.grid(row=1, column=1, columnspan=3, padx=2, sticky="we")
        self.wechat_enable = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="启用企业微信", variable=self.wechat_enable).grid(row=1, column=4, padx=2)
        ttk.Button(row2, text="测试企业微信", command=self.test_wechat, width=8).grid(row=1, column=5, padx=2)

        # 第三行：操作按钮
        row3 = ttk.Frame(control_frame)
        row3.pack(fill=tk.X, pady=3)
        self.start_btn = ttk.Button(row3, text="▶ 启动", command=self.start_monitor)
        self.start_btn.pack(side=tk.LEFT, padx=2)
        self.stop_btn = ttk.Button(row3, text="⏹ 停止", command=self.stop_monitor, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=2)
        ttk.Button(row3, text="💾 保存", command=self.save_config).pack(side=tk.LEFT, padx=2)
        
        ttk.Button(row3, text="⚠️ 急停", command=self.emergency_stop, style='Emergency.TButton').pack(side=tk.LEFT, padx=2)
        ttk.Button(row3, text="🔍 查询历史记录", command=self.show_history).pack(side=tk.LEFT, padx=2)

        # 样式配置
        self.style = ttk.Style()
        self.style.configure('Emergency.TButton', foreground='white', background='red')

        #创建监控策略及高级配置面板
        frame = ttk.Frame(control_frame)
        frame.pack(pady=10, fill=tk.X, padx=5)

         # 价格监控配置 
        price_frame = ttk.Frame(frame)
        price_frame.grid(row=0, column=0, padx=5, pady=5, sticky=tk.W)
        ttk.Label(price_frame, text="价格监控").grid(row=0, column=0, sticky=tk.W)

        # 启用价格监控的开关
        self.price_enable = tk.BooleanVar(value=True)
        ttk.Checkbutton(price_frame, text="启用", variable=self.price_enable).grid(row=0, column=1)

        # 价格监控方向选择（双向、仅涨、仅跌）
        self.price_direction = tk.StringVar(value='both')
        ttk.Radiobutton(price_frame, text="双向", variable=self.price_direction, value='both').grid(row=0, column=2)
        ttk.Radiobutton(price_frame, text="仅涨", variable=self.price_direction, value='up').grid(row=0, column=3)
        ttk.Radiobutton(price_frame, text="仅跌", variable=self.price_direction, value='down').grid(row=0, column=4)

        # 周期设置
        ttk.Label(price_frame, text="监控周期:").grid(row=0, column=5)
        self.price_tf = ttk.Combobox(price_frame, values=['1m', '5m', '15m', '30m', '60m'], width=5)
        self.price_tf.set('5m')  # 默认5分钟
        self.price_tf.grid(row=0, column=6)

        # 阈值输入
        ttk.Label(price_frame, text="涨跌阈值(%)：").grid(row=0, column=7)
        self.price_threshold = ttk.Entry(price_frame, width=5)
        self.price_threshold.insert(0, "5.0")  # 默认 5%
        self.price_threshold.grid(row=0, column=8)

        # 保存按钮
        ttk.Button(price_frame, text="保存价格监控设置", command=self.save_price_monitor_config).grid(row=0, column=10, padx=5, pady=3, sticky="e")

        # 均线策略配置（支持多组策略）
        ma_frame = ttk.Frame(frame)
        ma_frame.grid(row=1, column=0, padx=5, pady=5, sticky=tk.W)
        ttk.Label(ma_frame, text="均线策略").grid(row=0, column=0, sticky=tk.W)
        self.enable_bullish_ma = tk.BooleanVar(value=False)
        self.enable_bearish_ma = tk.BooleanVar(value=False)
        ttk.Checkbutton(ma_frame, text="启用多头排列策略", variable=self.enable_bullish_ma).grid(row=1, column=3, padx=5, pady=2, sticky=tk.W)
        ttk.Checkbutton(ma_frame, text="启用空头排列策略", variable=self.enable_bearish_ma).grid(row=1, column=4, padx=5, pady=2, sticky=tk.W)
          # 添加保存均线策略设置按钮
        ttk.Button(ma_frame, text="保存均线策略设置", command=self.save_ma_strategy_config)\
            .grid(row=0, column=10, padx=5, pady=3, sticky="e")
        self.ma_configs = []
        for i in range(2):  # 支持两组均线策略
            config_frame = ttk.Frame(ma_frame)
            config_frame.grid(row=i, column=1, sticky=tk.W)
            enable_var = tk.BooleanVar(value=(i==0))
            ttk.Checkbutton(config_frame, text=f"策略{i+1}", variable=enable_var).grid(row=0, column=0)
            entries = []
            for j, period in enumerate([5, 20, 60]):
                entry = ttk.Entry(config_frame, width=3)
                entry.insert(0, str(period))
                entry.grid(row=0, column=j*2+1)
                entries.append(entry)
                if j < 2:
                    ttk.Label(config_frame, text="/").grid(row=0, column=j*2+2)
            timeframe = ttk.Combobox(config_frame, values=['1f', '5f', '15f','1h','2h', '4h', '6h','1d'], width=4)
            timeframe.set('1h')
            timeframe.grid(row=0, column=7)
            self.ma_configs.append({
                'enable': enable_var,
                'short': entries[0],
                'medium': entries[1],
                'long': entries[2],
                'timeframe': timeframe
            })
        """
        创建包含排除交易对管理和单个交易对监控的区域，
        使用水平 Panedwindow 将两部分并排显示。
        """
        # 创建一个 LabelFrame 用于整体区域
        settings_frame = ttk.LabelFrame(parent, text="监控策略设置")
        settings_frame.pack(fill=tk.X, padx=5, pady=5)
    
        # 创建水平分割的 Panedwindow
        paned = ttk.Panedwindow(settings_frame, orient=tk.HORIZONTAL)
        paned.pack(expand=True, fill=tk.BOTH, padx=5, pady=5)
    
        # 左侧：排除交易对管理（取自你原有的 exclude_frame 代码）
        exclude_frame = ttk.LabelFrame(paned, text="排除交易对管理")
        exclude_frame.grid(row=3, column=0, columnspan=4, padx=5, pady=5, sticky="ew")

        # 输入框：手动输入待添加的交易对
        self.exclude_entry = ttk.Entry(exclude_frame, width=15)
        self.exclude_entry.grid(row=0, column=0, padx=5, pady=3)

        # 为了方便选择，绑定按键事件动态更新下拉选项
        self.exclude_entry.bind("<KeyRelease>", self.update_exclude_dropdown)

        # 下拉选择框：显示已有交易对匹配输入
        self.exclude_dropdown = ttk.Combobox(exclude_frame, width=15)
        self.exclude_dropdown.grid(row=0, column=1, padx=5, pady=3)

        # 当用户从下拉选项中选择后，将选中的交易对填入输入框
        self.exclude_dropdown.bind("<<ComboboxSelected>>", lambda e: self.exclude_entry.delete(0, tk.END) or self.exclude_entry.insert(0, self.exclude_dropdown.get()))

        # 添加按钮：确认添加
        ttk.Button(exclude_frame, text="添加", command=self.add_excluded_pair).grid(row=0, column=2, padx=5, pady=3)

        # 删除按钮：删除选中的交易对
        ttk.Button(exclude_frame, text="删除", command=self.remove_excluded_pair).grid(row=0, column=3, padx=5, pady=3)

        # 列表框：显示已添加的排除交易对
        self.exclude_listbox = tk.Listbox(exclude_frame, height=5)
        self.exclude_listbox.grid(row=1, column=0, columnspan=4, padx=5, pady=5, sticky="we")

        # 添加保存排除交易对设置按钮
        ttk.Button(exclude_frame, text="保存排除设置", command=self.save_excluded_pairs_config).grid(row=0, column=4, padx=5, pady=3, sticky="e")

        paned.add(exclude_frame, weight=1)
    
        # 右侧：单个交易对监控区域
        single_pair_frame = ttk.LabelFrame(paned, text="单个交易对监控")
        # 交易对输入，使用 Combobox 以便自动提示（参见问题二）
        ttk.Label(single_pair_frame, text="交易对：").grid(row=0, column=0, padx=5, pady=3, sticky=tk.W)
        self.single_pair_combo = ttk.Combobox(single_pair_frame, width=15)
        self.single_pair_combo.grid(row=0, column=1, padx=5, pady=3)
        self.single_pair_combo.bind("<KeyRelease>", self.update_single_pair_dropdown)
        # 关键：必须赋值给 self.single_pair_listbox
        self.single_pair_listbox = tk.Listbox(single_pair_frame, height=5)
        self.single_pair_listbox.grid(row=1, column=0, columnspan=3, padx=5, pady=5, sticky="ew")
        # 添加保存单个交易对监控设置按钮
        ttk.Button(single_pair_frame, text="保存单对监控设置", command=self.save_single_pair_monitor_config)\
            .grid(row=2, column=0, columnspan=3, padx=5, pady=3, sticky="e")
        # 策略1：均线监控参数
        ttk.Label(single_pair_frame, text="策略1 - 时间周期：").grid(row=3, column=0, padx=5, pady=3, sticky=tk.E)
        self.sp_timeframe_entry = ttk.Entry(single_pair_frame, width=8)
        self.sp_timeframe_entry.insert(0, "5m")
        self.sp_timeframe_entry.grid(row=3, column=1, padx=3, pady=3)
    
        ttk.Label(single_pair_frame, text="均线周期：").grid(row=3, column=2, padx=2, pady=1, sticky=tk.E)
        self.sp_ma_period_entry = ttk.Entry(single_pair_frame, width=8)
        self.sp_ma_period_entry.insert(0, "20")
        self.sp_ma_period_entry.grid(row=3, column=3, padx=2, pady=2)
    
        ttk.Label(single_pair_frame, text="偏离阈值(%)：").grid(row=3, column=4, padx=5, pady=3, sticky=tk.E)
        self.sp_ma_threshold_entry = ttk.Entry(single_pair_frame, width=8)
        self.sp_ma_threshold_entry.insert(0, "5")
        self.sp_ma_threshold_entry.grid(row=3, column=5, padx=5, pady=3)
    
        # 策略2：涨跌幅监控参数
        ttk.Label(single_pair_frame, text="策略2 - 时间周期：").grid(row=4, column=0, padx=5, pady=3, sticky=tk.E)
        self.sp_vol_timeframe_entry = ttk.Entry(single_pair_frame, width=8)
        self.sp_vol_timeframe_entry.insert(0, "5m")
        self.sp_vol_timeframe_entry.grid(row=4, column=1, padx=5, pady=3)
    
        ttk.Label(single_pair_frame, text="涨跌幅阈值(%)：").grid(row=4, column=2, padx=5, pady=3, sticky=tk.E)
        self.sp_vol_threshold_entry = ttk.Entry(single_pair_frame, width=8)
        self.sp_vol_threshold_entry.insert(0, "10")
        self.sp_vol_threshold_entry.grid(row=4, column=3, padx=5, pady=3)
    
        # 启用/禁用按钮
        self.enable_single_pair_btn = ttk.Button(single_pair_frame, text="启用监控", command=self.enable_single_pair_strategy)
        self.enable_single_pair_btn.grid(row=1, column=3, padx=5, pady=5)
        self.disable_single_pair_btn = ttk.Button(single_pair_frame, text="禁用监控", command=self.disable_single_pair_strategy)
        self.disable_single_pair_btn.grid(row=2, column=3, padx=5, pady=5)
    
        paned.add(single_pair_frame, weight=1)
#=========================================================#
        # 高级配置
        adv_frame = ttk.Frame(frame)
        adv_frame.grid(row=2, column=0, padx=5, pady=5, sticky=tk.W)
        ttk.Label(adv_frame, text="最大保留:").grid(row=0, column=0)
        self.data_retention = ttk.Combobox(adv_frame, values=['1小时', '6小时', '24小时'], width=8)
        self.data_retention.set('6小时')
        self.data_retention.grid(row=0, column=1)
        ttk.Label(adv_frame, text="内存清理间隔(s):").grid(row=0, column=6)
        self.mem_interval = ttk.Entry(adv_frame, width=6)
        self.mem_interval.insert(0, "600")
        self.mem_interval.grid(row=0, column=7)
        ttk.Label(adv_frame, text="最大交易对数:").grid(row=0, column=8)
        self.max_pairs = ttk.Entry(adv_frame, width=6)
        self.max_pairs.insert(0, "500")
        self.max_pairs.grid(row=0, column=9)
        ttk.Label(adv_frame, text="监控间隔(s):").grid(row=0, column=4)
        self.check_interval = ttk.Entry(adv_frame, width=6)
        self.check_interval.insert(0, "300")
        self.check_interval.grid(row=0, column=5)
        # 高级配置（在已有控件后添加警报冷却时间设置）
        ttk.Label(adv_frame, text="均线排列监控冷却时间(s):").grid(row=0, column=10)
        self.alert_cooldown_entry = ttk.Entry(adv_frame, width=6)
        self.alert_cooldown_entry.insert(0, "6000")  # 默认300秒
        self.alert_cooldown_entry.grid(row=0, column=11)

        #创建底部状态栏
        status_frame = ttk.Frame(control_frame)
        status_frame.pack(fill=tk.X, padx=5, pady=5)
        status_items = [
            ("连接成功率:", 'success_rate_label', '0.00%'),
            ("监控交易对:", 'monitored_pairs_label', '0'),
            ("内存记录:", 'mem_usage_label', '0 MB'),
            ("最后更新:", 'last_update_label', '--:--:--'),
            ("警报队列:", 'alert_queue_label', '0')
        ]
        for idx, (text, var_name, default) in enumerate(status_items):
            ttk.Label(status_frame, text=text).grid(row=0, column=idx*2, padx=5)
            setattr(self, var_name, ttk.Label(status_frame, text=default))
            getattr(self, var_name).grid(row=0, column=idx*2+1, padx=5)
        

    def toggle_proxy_entry(self):
        if self.proxy_enable.get():
            self.proxy_entry.config(state=tk.NORMAL)
        else:
            self.proxy_entry.config(state=tk.DISABLED)

    #================================================================#
    # 【单独保存设置功能：各模块各自读取控件，并调用对应监控对象更新接口】

    def save_price_monitor_config(self):
        """保存价格监控相关设置"""
        config_section = {
            'enable_price_monitor': self.price_enable.get(),
            'price_timeframe': self.price_tf.get(),# 获取选择的时间周期（1m, 5m, 15m等）
            'price_threshold': float(self.price_threshold.get()),
            'price_direction': self.price_direction.get()
        }
        if self.monitor and hasattr(self.monitor, "update_config"):
            # 假设 update_config 会更新相关参数，
            # 你可以设计 update_config 支持局部更新（例如只更新价格监控相关项）
            self.monitor.update_config(config_section)
        self.log_message("价格监控设置已更新", "log")

    def save_ma_strategy_config(self):
        """保存均线策略监控设置"""
        # 假设均线策略配置存放在 ma_configs 控件中
        ma_strategies = []
        for cfg in self.ma_configs:
            if cfg['enable'].get():
                try:
                    periods = [int(cfg['short'].get()),
                               int(cfg['medium'].get()),
                               int(cfg['long'].get())]
                    ma_strategies.append({
                        'periods': periods,
                        'timeframe': cfg['timeframe'].get()
                    })
                except ValueError:
                    self.log_message("均线策略输入格式错误", "warning")
        if self.monitor and hasattr(self.monitor, "update_config"):
            self.monitor.update_config({'ma_strategies': ma_strategies})
        self.log_message("均线策略设置已更新", "log")

    def save_excluded_pairs_config(self):
        """保存排除交易对管理设置"""
        excluded_pairs_list = list(self.exclude_listbox.get(0, tk.END))
        if self.monitor and hasattr(self.monitor, "update_config"):
            self.monitor.update_config({'excluded_pairs': ",".join(excluded_pairs_list)})
        self.log_message("排除交易对设置已更新", "log")

    def save_single_pair_monitor_config(self):
        """保存单个交易对监控设置（单独更新监控列表及参数）"""
        if not hasattr(self, 'single_pair_listbox'):
            self.log_message("错误：未找到单个交易对监控列表控件", "warning")
            return
        # 读取 Listbox 中存储的单个交易对监控列表
        pair_list = list(self.single_pair_listbox.get(0, tk.END))
        # 更新监控对象中单个交易对监控的列表
        if self.monitor and hasattr(self.monitor, "update_single_pair_list"):
            self.monitor.update_single_pair_list(pair_list)
        # 如果单个交易对监控区域中还包含其他参数，
        # 例如策略1和策略2的默认参数（这里假设这些参数在各对应的 Entry 控件中设置），
        # 你也可以构造一个统一的配置字典并传递给监控对象。
        self.log_message("单个交易对监控设置已更新", "log")

    # 保存配置时，将控件的值写入配置字典，例如：
    def save_stock_monitor_config(self):
        """保存股票监控设置，包括股票代码列表及其他参数"""
        stock_list = list(self.stock_listbox.get(0, tk.END))
        stock_config = {
            'stock_list': ",".join(stock_list),
            'stock_period': self.stock_period.get(),  # 这里保存数据周期
            'ma_period': self._safe_get(int, self.ma_period_entry, 30),
            'ma_threshold': self._safe_get(float, self.ma_threshold_entry, 2.0),
            'stock_check_interval': self._safe_get(int, self.stock_interval_entry, 60)
        }
        self.stock_monitor = StockMonitorPro(stock_config, log_callback=self.log_message)
        if self.stock_monitor:
            if hasattr(self.stock_monitor, "update_config"):
                self.stock_monitor.update_config(stock_config)
            if hasattr(self.stock_monitor, "update_stock_list"):
                self.stock_monitor.update_stock_list(stock_list)
        self.log_message("股票监控设置已更新", "log")

    def save_all_configs(self):
        """
        整体保存配置：
         - 保存全局配置到文件
         - 同时分别更新价格监控、均线策略、排除交易对、单个交易对监控、股票监控的配置
        """
        try:
            # 先获取所有配置（你原有 get_config() 方法返回的是全局配置）
            config = self.get_config()
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            self.log_message("全局配置保存成功", "log")
            # 分模块更新
            self.save_price_monitor_config()
            self.save_ma_strategy_config()
            self.save_excluded_pairs_config()
            self.save_single_pair_monitor_config()
            self.save_stock_monitor_config()
        except Exception as e:
            messagebox.showerror("保存错误", str(e))
    #=================================================================
    def fetch_stock_codes(self):
        # 获取个股代码列表（调用你实际的数据接口，这里使用示例函数
        codes = fetch_all_stock_codes()  # 请确保 fetch_all_stock_codes 在本模块或已导入
        # 将返回的代码列表填充到股票下拉框中
        self.stock_combo['values'] = codes
        self.log_message("股票代码列表已更新", "log")

    def on_stock_selected(self, event=None):
        """
        当用户选择某个股票代码时，立即预加载数据
        """
        symbol = self.stock_combo.get().strip()
        print("stock_monitor =", self.stock_monitor)
        if symbol:
            if self.stock_monitor is None:
                self.log_message("股票监控模块未启动，请先启动股票监控！", "warning")
                return
            threading.Thread(target=self.stock_monitor.preload_stock_data, args=(symbol,), daemon=True).start()
    #=================================================================================================
    def enable_single_pair_strategy(self):
        pair = self.single_pair_entry.get().strip()
        timeframe = self.sp_timeframe_entry.get().strip()
        ma_period = int(self.sp_ma_period_entry.get())
        ma_threshold = float(self.sp_ma_threshold_entry.get())
        vol_timeframe = self.sp_vol_timeframe_entry.get().strip()
        vol_threshold = float(self.sp_vol_threshold_entry.get())
        if not pair:
            self.log_message("请输入交易对", "warning")
            return
        # 你可以将这些参数保存到 monitor 对象中，或者新建一个监控线程来监控该交易对
        if self.monitor and hasattr(self.monitor, "enable_single_pair_strategy"):
            self.monitor.enable_single_pair_strategy(pair,
                                                 strategy1={'timeframe': timeframe,
                                                            'ma_period': ma_period,
                                                            'threshold': ma_threshold},
                                                 strategy2={'timeframe': vol_timeframe,
                                                            'threshold': vol_threshold})
            self.log_message(f"已启用 {pair} 单对监控", "log")
        else:
            self.log_message("监控系统未启动或不支持单对监控", "warning")

    def disable_single_pair_strategy(self):
        pair = self.single_pair_entry.get().strip()
        if not pair:
            self.log_message("请输入交易对", "warning")
            return
        if self.monitor and hasattr(self.monitor, "disable_single_pair_strategy"):
            self.monitor.disable_single_pair_strategy(pair)
            self.log_message(f"已禁用 {pair} 单对监控", "log")
        else:
            self.log_message("监控系统未启动或不支持单对监控", "warning")

    
    def update_single_pair_dropdown(self, event=None):
        user_input = self.single_pair_combo.get().upper().strip()
        available_pairs = []
        # 假设 monitor.usdt_pairs_cache 存放所有交易对列表（来自各交易所）
        if self.monitor:
            for pairs in self.monitor.usdt_pairs_cache.values():
                available_pairs.extend(pairs)
        available_pairs = list(set(available_pairs))
        # 筛选以输入内容开头的
        matched = [p for p in available_pairs if p.upper().startswith(user_input)]
        self.single_pair_combo['values'] = matched

    def add_single_pair(self):
        pair = self.single_pair_combo.get().upper().strip()
        if not pair:
            self.log_message("请输入交易对代码", "warning")
            return
        existing = self.single_pair_listbox.get(0, tk.END)
        if pair in existing:
            self.log_message(f"{pair} 已在监控列表中", "warning")
            return
        self.single_pair_listbox.insert(tk.END, pair)
        self.log_message(f"添加 {pair} 到监控列表", "log")

    def remove_single_pair(self):
        selected = self.single_pair_listbox.curselection()
        if not selected:
            self.log_message("请先选择要删除的交易对", "warning")
            return
        for idx in reversed(selected):
            self.single_pair_listbox.delete(idx)
            self.log_message("删除监控列表中的交易对", "log")

    def enable_all_single_pair_strategies(self):
        pairs = list(self.single_pair_listbox.get(0, tk.END))
        # 读取策略参数（你可以设置统一参数，也可以为每个交易对单独设置）
        # 例如：
        strategy1 = {
            "timeframe": self.sp_timeframe_entry.get().strip(),
            "ma_period": int(self.sp_ma_period_entry.get()),
            "threshold": float(self.sp_ma_threshold_entry.get())
        }
        strategy2 = {
            "timeframe": self.sp_vol_timeframe_entry.get().strip(),
            "threshold": float(self.sp_vol_threshold_entry.get())
        }
        if self.monitor and hasattr(self.monitor, "enable_single_pair_strategy"):
            for pair in pairs:
                self.monitor.enable_single_pair_strategy(pair, strategy1, strategy2)
            self.log_message("已启用所有单对监控策略", "log")
        else:
            self.log_message("监控系统未启动或不支持单对监控", "warning")

    def disable_all_single_pair_strategies(self):
        pairs = list(self.single_pair_listbox.get(0, tk.END))
        if self.monitor and hasattr(self.monitor, "disable_single_pair_strategy"):
            for pair in pairs:
                self.monitor.disable_single_pair_strategy(pair)
            self.log_message("已禁用所有单对监控策略", "log")
        else:
            self.log_message("监控系统未启动或不支持单对监控", "warning")
    #===================================================================================


    def add_excluded_pair(self):
        """
        从输入框中获取交易对代码，确认后添加到列表中（如果不重复）。
        """
        pair = self.exclude_entry.get().strip().upper()
        if not pair:
            self.log_message("请输入交易对代码", "warning")
            return
        # 检查是否已经添加
        existing = self.exclude_listbox.get(0, tk.END)
        if pair in existing:
            self.log_message(f"{pair} 已在排除列表中", "warning")
            return
        self.exclude_listbox.insert(tk.END, pair)
        self.exclude_entry.delete(0, tk.END)
        self.log_message(f"已添加 {pair} 到排除列表", "log")

    def remove_excluded_pair(self):
        """
        删除列表框中选中的交易对
        """
        selected_indices = self.exclude_listbox.curselection()
        if not selected_indices:
            self.log_message("请先选择要删除的交易对", "warning")
            return
        # 反向删除，避免索引问题
        for idx in reversed(selected_indices):
            self.exclude_listbox.delete(idx)
            self.log_message("交易对已从排除列表移除", "log")

    def update_exclude_dropdown(self, event=None):
        """
        当用户在排除交易对输入框中输入内容时，
        根据当前监控系统中的交易对列表或已有排除列表，动态更新下拉框选项。
        """
        user_input = self.exclude_entry.get().upper().strip()
        # 假设你的交易对数据可以从监控系统中获取，比如：
        available_pairs = []
        if self.monitor:  # 如果虚拟货币监控模块已经启动
            # 例如，假设 self.monitor.usdt_pairs_cache 存储了当前所有交易对
            for pair_list in self.monitor.usdt_pairs_cache.values():
                available_pairs.extend(pair_list)
        # 去重
        available_pairs = list(set(available_pairs))
        # 筛选出以输入字符开头的交易对
        matched = [p for p in available_pairs if p.upper().startswith(user_input)]
        # 更新下拉框选项
        self.exclude_dropdown['values'] = matched

#========================================================================================
    def _create_stock_page(self, parent):
        """构建股票监控页面，将股票监控相关控件放入此页面"""

        # 股票列表管理
        stock_list_frame = ttk.LabelFrame(self.stock_frame, text="监控股票列表")
        stock_list_frame.grid(row=0, column=0, padx=5, pady=5, sticky="ew")
        # 在股票监控区域创建输入控件和下拉框
        ttk.Label(stock_list_frame, text="股票代码：").grid(row=0, column=0, padx=5, pady=3, sticky="w")
        self.stock_entry = ttk.Entry(stock_list_frame, width=15)  # 手动输入
        self.stock_entry.grid(row=0, column=1, padx=5, pady=3)
        ttk.Label(stock_list_frame, text="或选择：").grid(row=0, column=2, padx=5, pady=3, sticky="w")
        self.stock_combo = ttk.Combobox(stock_list_frame, width=15)
        self.stock_combo.grid(row=0, column=0, padx=5, pady=3)
        self.stock_combo.bind("<KeyRelease>", self.update_stock_dropdown)
        self.stock_combo.bind("<<ComboboxSelected>>", self.on_stock_selected)

        # 添加一个按钮，用于提前获取股票代码列表
        ttk.Button(stock_list_frame, text="获取个股列表", command=self.fetch_stock_codes).grid(row=1, column=4, padx=5, pady=3)

        ttk.Button(stock_list_frame, text="添加", command=self.add_stock).grid(row=0, column=3, padx=5)
        ttk.Button(stock_list_frame, text="删除", command=self.remove_stock).grid(row=0, column=4, padx=5)
        self.stock_listbox = tk.Listbox(stock_list_frame, height=5)
        self.stock_listbox.grid(row=1, column=0, columnspan=3, padx=5, pady=5, sticky="ew")

        # 监控参数设置
        settings_frame = ttk.LabelFrame(self.stock_frame, text="监控参数")
        settings_frame.grid(row=1, column=0, padx=5, pady=5, sticky="ew")
    
        ttk.Label(settings_frame, text="数据周期：").grid(row=0, column=0, padx=5, sticky=tk.W)

        # 创建 Combobox 控件，默认选择 daily
        self.stock_period = ttk.Combobox(settings_frame, values=["5min", "15min", "30min", "60min", "120min", "240min", "daily"], width=10)
        self.stock_period.set("daily")  # 默认值
        self.stock_period.grid(row=0, column=1, padx=5)
    
        ttk.Label(settings_frame, text="均线周期:").grid(row=0, column=2, padx=5, pady=3)
        self.ma_period_entry = ttk.Entry(settings_frame, width=5)
        self.ma_period_entry.insert(0, "30")
        self.ma_period_entry.grid(row=0, column=3, padx=5, pady=3)
    
        ttk.Label(settings_frame, text="距离均线阈值(%):").grid(row=0, column=4, padx=5, pady=3)
        self.ma_threshold_entry = ttk.Entry(settings_frame, width=5)
        self.ma_threshold_entry.insert(0, "2.0")
        self.ma_threshold_entry.grid(row=0, column=5, padx=5, pady=3)
    
        ttk.Label(settings_frame, text="检查间隔(s):").grid(row=0, column=6, padx=5, pady=3)
        self.stock_interval_entry = ttk.Entry(settings_frame, width=5)
        self.stock_interval_entry.insert(0, "60")
        self.stock_interval_entry.grid(row=0, column=7, padx=5, pady=3)
        # 添加保存股票监控设置按钮
        ttk.Button(settings_frame, text="保存股票监控设置", command=self.save_stock_monitor_config)\
            .grid(row=0, column=8, padx=5, pady=3, sticky="e")

        # 新增检测网络连接按钮
        ttk.Button(settings_frame, text="检测股票网络连接", command=self.test_stock_network).grid(row=0, column=8, padx=5, pady=3)
    
        # 启动/停止按钮
        self.stock_start_btn = ttk.Button(self.stock_frame, text="启动股票监控", command=self.start_stock_monitor)
        self.stock_start_btn.grid(row=2, column=0, padx=5, pady=5, sticky="w")
        self.stock_stop_btn = ttk.Button(self.stock_frame, text="停止股票监控", command=self.stop_stock_monitor, state=tk.DISABLED)
        self.stock_stop_btn.grid(row=2, column=0, padx=5, pady=5, sticky="e")

    def add_stock(self):
        # 尝试先从下拉框中获取值，如果下拉框为空，则使用手动输入的 Entry
        symbol = self.stock_combo.get().strip().lower()
        if not symbol:
            symbol = self.stock_entry.get().strip().lower()
        if not symbol:
            self.log_message("股票代码为空", "warning")
            return
        # 检查是否已经添加
        existing = self.stock_listbox.get(0, tk.END)
        if symbol in existing:
            self.log_message(f"{symbol} 已在监控列表中", "warning")
            return
        self.stock_listbox.insert(tk.END, symbol)
        # 清空输入框（可选）
        self.stock_entry.delete(0, tk.END)
        self.stock_combo.set("")
        self.log_message(f"已添加股票 {symbol}", "log")

    def remove_stock(self):
        selected = self.stock_listbox.curselection()
        for idx in reversed(selected):
            self.stock_listbox.delete(idx)

    def start_stock_monitor(self):
        try:
            # 从列表框中获取股票列表
            stock_list = list(self.stock_listbox.get(0, tk.END))
            if not stock_list:
                self.log_message("请添加股票代码", "warning")
                return
            stock_period = self.stock_period.get()
            ma_period = int(self.ma_period_entry.get())
            ma_threshold = float(self.ma_threshold_entry.get())
            stock_interval = int(self.stock_interval_entry.get())
            config = {
                'stock_list': stock_list,
                'stock_period': stock_period,
                'ma_period': ma_period,
                'ma_threshold': ma_threshold,
                'stock_check_interval': stock_interval
            }
            from threading import Thread
            # 创建股票监控实例（此时 StockMonitorPro 内部延迟导入 akshare 也可保证）
            self.stock_monitor = StockMonitorPro(config, self.log_message)
            # 启动独立线程运行监控
            self.stock_monitor_thread = Thread(target=self.stock_monitor.start_monitoring, daemon=True)
            self.stock_monitor_thread.start()
            self.stock_start_btn.config(state=tk.DISABLED)
            self.stock_stop_btn.config(state=tk.NORMAL)
            self.log_message("股票监控启动", "log")
        except Exception as e:
            self.log_message(f"启动股票监控异常: {e}", "warning")

    def stop_stock_monitor(self):
        if hasattr(self, 'stock_monitor'):
            self.stock_monitor.running = False
            self.stock_start_btn.config(state=tk.NORMAL)
            self.stock_stop_btn.config(state=tk.DISABLED)
            self.log_message("股票监控已停止", "log")
    
    def test_stock_network(self):
        """
        测试股票监控的网络连接，此处以访问东财的股票数据接口为例
        （你也可以选择其他稳定的 URL，比如百度）。
        """
        test_url = "https://push2.eastmoney.com/api/qt/stock/kline/get"  # 示例 URL
        try:
            response = requests.get(test_url, timeout=5)
            if response.status_code == 200:
                self.log_message("股票监控网络连接测试成功", "log")
            else:
                self.log_message(f"股票监控网络连接失败，状态码: {response.status_code}", "warning")
        except Exception as e:
            self.log_message(f"股票监控网络连接异常: {e}", "warning")
    #==================================================================

    def update_stock_dropdown(self, event=None):
        # 当用户在股票下拉框中输入时，可以进行过滤（示例：简单过滤）
        user_input = self.stock_combo.get().strip().upper()
        if not user_input:
            return
        # 这里假设 self.stock_combo['values'] 已经设置为一个列表
        all_codes = self.stock_combo['values']
        matched = [code for code in all_codes if code.upper().startswith(user_input)]
        self.stock_combo['values'] = matched
    
    def fetch_all_stock_codes_async(callback):
        """
        在后台线程中获取股票代码，并通过 callback(codes) 将结果传回。
        callback 应该是一个函数，接受一个列表参数（股票代码列表）。
        """
        def task():
            codes = fetch_all_stock_codes()
            callback(codes)
        threading.Thread(target=task, daemon=True).start()

    #==================================================================
    
    def _create_log_and_leaderboard_panel(self):
        """在 Notebook 下方创建统一的日志与排行榜区域"""
        panel = ttk.LabelFrame(self, text="实时日志与监控排行榜")
        panel.pack(fill=tk.BOTH, padx=5, pady=5, expand=True)
        paned = ttk.Panedwindow(panel, orient=tk.HORIZONTAL)
        paned.pack(expand=True, fill=tk.BOTH)
        # 日志区域
        log_frame = ttk.Frame(paned)
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD,
                                                   font=('Consolas', 9),
                                                   padx=5, pady=5,
                                                   state='disabled')
        self.log_area.pack(expand=True, fill=tk.BOTH)
        paned.add(log_frame, weight=3)
        # 排行榜区域
        leaderboard_frame = ttk.Frame(paned)
        self.leaderboard_tree = ttk.Treeview(leaderboard_frame, columns=("symbol", "change"), show='headings', height=15)
        self.leaderboard_tree.heading("symbol", text="交易对")
        self.leaderboard_tree.heading("change", text="涨跌幅(%)")
        self.leaderboard_tree.column("symbol", width=100, anchor='center')
        self.leaderboard_tree.column("change", width=80, anchor='center')
        self.leaderboard_tree.pack(expand=True, fill=tk.BOTH)
        paned.add(leaderboard_frame, weight=1)
        # 日志区域颜色配置
        self.log_area.tag_config('log', foreground='gray')
        self.log_area.tag_config('price', foreground='blue')
        self.log_area.tag_config('bullish', foreground='green')
        self.log_area.tag_config('bearish', foreground='red')
        self.log_area.tag_config('warning', foreground='orange')


    def show_history(self):
        """弹出窗口显示历史记录"""
        if not self.monitor:
            self.log_message("监控未启动，无历史记录可查询", 'warning')
            return

        history_records = self.monitor.history.records
        if not history_records:
            messagebox.showinfo("历史记录", "当前没有历史记录")
            return

        # 创建一个新窗口显示历史记录
        history_win = tk.Toplevel(self)
        history_win.title("历史记录查询")
        history_win.geometry("800x600")
    
        # 使用ScrolledText显示记录
        st = scrolledtext.ScrolledText(history_win, wrap=tk.WORD, font=('Consolas', 10))
        st.pack(expand=True, fill=tk.BOTH, padx=5, pady=5)
    
        # 将记录逐行写入
        for record in history_records:
            line = f"[{record['timestamp']}] {record['exchange'].upper()} {record['symbol']} - {record['message']} ({record['type']})\n"
            st.insert(tk.END, line)
        st.configure(state='disabled')


    def _create_status_light(self, text: str) -> tk.Label:
        """创建状态指示灯控件"""
        frame = ttk.Frame(self.status_frame)
        frame.pack(side=tk.LEFT, padx=5)
        ttk.Label(frame, text=text).pack(side=tk.LEFT)
        indicator = tk.Label(frame, text="●", font=('Arial', 12), fg="gray")
        indicator.pack(side=tk.LEFT)
        return indicator
    
    def update_leaderboard(self):
        """
        更新排行榜 GUI 显示
        """
        if not hasattr(self, 'monitor') or self.monitor is None:
            self.log_message("错误：监控系统未初始化", "warning")
            return
        
        leaderboard_data = self.monitor.get_leaderboard()  # 获取排行榜数据
        leaderboard = leaderboard_data["leaderboard"]  # 获取涨幅列表

        # 清空 GUI 列表
        self.leaderboard_listbox.delete(0, tk.END)

        for entry in leaderboard:
            symbol = entry.get("symbol", "未知")
            change = entry.get("change", 0.0)
            exchange = entry.get("exchange", "未知")
            display_text = f"{symbol} ({exchange}) 涨幅: {change:.2f}%"
        
            # 添加到 GUI
            self.leaderboard_listbox.insert(tk.END, display_text)


    def process_alerts(self):
        """处理警报队列，将消息追加到日志面板"""
        try:
            while self._pending_alerts:
                alert = self._pending_alerts.popleft()
                self.log_area.configure(state='normal')
                # 添加颜色标签
                tag = alert['type']
                self.log_area.insert(tk.END, alert['message'] + "\n", tag)
                self.log_area.see(tk.END)
                self.log_area.configure(state='disabled')
        except Exception as e:
            print("处理日志异常:", e)
        finally:
            self.after(500, self.process_alerts)

    def log_message(self, message: str, category: str = 'log') -> None:
        """将日志消息添加到待处理队列"""
        self._pending_alerts.append({'message': message, 'type': category})
        # 强制GUI更新（解决日志延迟）
        self.event_generate("<<LogUpdate>>", when="tail")

    def update_status(self) -> None:
        """定时更新界面状态信息"""
        if self.monitor:
            # 更新交易所状态指示灯
            for ex_id, indicator in [('binance', self.binance_indicator),
                                     ('okx', self.okx_indicator),
                                     ('htx', self.htx_indicator)]:
                status = self.monitor.exchange_status.get(ex_id, 'disconnected')
                color = 'green' if status == 'connected' else 'red'
                indicator.config(fg=color, text=f"{ex_id.upper()}: {status}")
            # 更新连接统计信息
            stats = self.monitor.connection_stats
            total = stats.get('total_pairs', 1)
            rate = (stats.get('success_pairs', 0) / total * 100) if total > 0 else 0
            self.success_rate_label.config(text=f"{rate:.2f}%")
            # 显示USDT交易对数量（从缓存中统计）
            monitored_pairs = sum(len(self.monitor.usdt_pairs_cache.get(ex, [])) for ex in self.monitor.exchanges)
            self.monitored_pairs_label.config(text=str(monitored_pairs))
            self.alert_queue_label.config(text=str(len(self._pending_alerts)))
            # 内存记录（使用警报历史记录条数）
            history_size = len(self.monitor.history.records)
            self.mem_usage_label.config(text=f"{history_size}条")
            if stats.get('last_update'):
                self.last_update_label.config(text=stats['last_update'].strftime("%H:%M:%S"))
        self.after(1000, self.update_status)

    def test_proxy(self):
        """测试代理连接是否正常"""
        proxy = self.proxy_entry.get().strip()
        if not proxy.startswith(('http://', 'https://')):
            self.log_message("代理格式必须以http://或https://开头", 'warning')
            return
        try:
            response = requests.get('https://api.binance.com/api/v3/ping',
                                    proxies={'http': proxy, 'https': proxy},
                                    timeout=10)
            if response.status_code == 200:
                self.log_message("代理连接测试成功", 'log')
            else:
                self.log_message(f"代理测试失败: {response.status_code}", 'warning')
        except Exception as e:
            self.log_message(f"代理测试异常: {str(e)}", 'warning')


    def test_exchange(self, exchange_name: str, url: str):
        """
        交易所检测函数：在后台线程中发起 HTTP 请求，更新日志和对应的状态指示标签。
        """
        def network_test():
            try:
                # 发起网络请求，设置超时和关闭 SSL 验证以防止异常
                response = requests.get(url, timeout=10, verify=False,
                                    proxies={'http': self.proxy_entry.get().strip(),
                                             'https': self.proxy_entry.get().strip()} if self.proxy_enable.get() else None)
                if response.status_code == 200:
                    self.log_message(f"{exchange_name} 网络检测成功", "log")
                    self.after(0, lambda: self.exchange_status_labels[exchange_name].config(text="已连接", foreground="green"))
                else:
                    self.log_message(f"{exchange_name} 网络检测失败，状态码: {response.status_code}", "warning")
                    self.after(0, lambda: self.exchange_status_labels[exchange_name].config(text="连接失败", foreground="red"))
            except Exception as e:
                self.log_message(f"{exchange_name} 网络检测异常: {str(e)}", "warning")
                self.after(0, lambda: self.exchange_status_labels[exchange_name].config(text="异常", foreground="red"))
        import threading
        threading.Thread(target=network_test, daemon=True).start()

    def test_telegram(self):
        """测试Telegram通知连接"""
        if not self.tg_enable.get():
            self.log_message("请先启用Telegram通知", 'warning')
            return
        notifier = TelegramNotifier(self.tg_token_entry.get().strip(),
                                    self.tg_chat_entry.get().strip())
        if notifier.test_connection():
            self.log_message("Telegram连接测试成功", 'log')
        else:
            self.log_message("Telegram连接测试失败", 'warning')

        
    def test_wechat(self):
        if not self.wechat_enable.get():
            self.log_message("请先启用企业微信提醒", 'warning')
            return
        notifier = EnterpriseWeChatNotifier(self.wechat_webhook_entry.get().strip())
        if notifier.test_connection():
            self.log_message("企业微信提醒连接测试成功", 'log')
        else:
            self.log_message("企业微信提醒连接测试失败", 'warning')

    def emergency_stop(self):
        """紧急停止监控"""
        if self.monitor:
            self.monitor.running.clear()
            self.monitor.optimizer.running = False
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.log_message("!! 紧急停止已触发 !!", 'warning')

    def get_config(self) -> dict:
        """从界面各控件获取配置参数，并进行验证"""
        config = {
            'exchanges': [],
            'proxy': self.proxy_entry.get().strip() if self.proxy_enable.get() else "",
            'check_interval': self._safe_get(int, self.check_interval, 300),
            'max_pairs': self._safe_get(int, self.max_pairs, 500),
            'enable_price_monitor': self.price_enable.get(),
            'price_timeframe': self.price_tf.get(),
            'price_threshold': self._safe_get(float, self.price_threshold, 5.0),
            'price_direction': self.price_direction.get(),
            'ma_strategies': [],
            'data_retention': self.data_retention.get(),
            'mem_interval': self._safe_get(int, self.mem_interval, 600),
            'enable_bullish_ma':self.enable_bullish_ma.get(),
            'enable_bearish_ma':self.enable_bearish_ma.get(),
            'enable_tg': self.tg_enable.get(),
            'enable_wechat': self.wechat_enable.get(),
            'wechat_webhook': self.wechat_webhook_entry.get().strip(),
            'tg_token': self.tg_token_entry.get().strip(),
            'tg_chat_id': self.tg_chat_entry.get().strip(),
            'alert_cooldown':self._safe_get(int, self.alert_cooldown_entry, 6000),
            # 股票监控配置
            'stock_list': ",".join(self.stock_listbox.get(0, tk.END)),
            'stock_period': self.stock_period.get(),
            'ma_period': self._safe_get(int, self.ma_period_entry, 30),
            'ma_threshold': self._safe_get(float, self.ma_threshold_entry, 2.0),
            'stock_check_interval': self._safe_get(int, self.stock_interval_entry, 60)
        }
        # 交易所选择
        for ex, var in [('binance', self.binance_var),
                        ('okx', self.okx_var),
                        ('htx', self.htx_var)]:
            if var.get():
                config['exchanges'].append(ex)
        # 均线策略配置
        for cfg in self.ma_configs:
            if cfg['enable'].get():
                try:
                    periods = [
                        int(cfg['short'].get()),
                        int(cfg['medium'].get()),
                        int(cfg['long'].get())
                    ]
                    config['ma_strategies'].append({
                        'periods': periods,
                        'timeframe': cfg['timeframe'].get()
                    })
                except ValueError:
                    pass

        # 获取排除交易对：从列表框中读取所有项目，并拼接成逗号分隔的字符串
        excluded_pairs_list = self.exclude_listbox.get(0, tk.END)
        config['excluded_pairs'] = ",".join(excluded_pairs_list)
        
        # 股票监控配置
        stock_list = list(self.stock_listbox.get(0, tk.END))
        config['stock_list'] = ",".join(stock_list)
        config['stock_period'] = self.stock_period.get()
        config['ma_period'] = self._safe_get(int, self.ma_period_entry, 30)
        config['ma_threshold'] = self._safe_get(float, self.ma_threshold_entry, 2.0)
        config['stock_check_interval'] = self._safe_get(int, self.stock_interval_entry, 60)


        # 验证配置合法性
        errors = self._validate_config(config)
        if errors:
            raise ValueError("配置错误:\n\n• " + "\n• ".join(errors))
        return config

    def _safe_get(self, dtype, widget, default):
        """安全转换控件输入为指定类型"""
        try:
            return dtype(widget.get())
        except (ValueError, AttributeError):
            return default

    def _validate_config(self, config: dict) -> list:
        """对用户配置进行基本验证"""
        errors = []
        if not config['exchanges']:
            errors.append("请至少选择一个交易所")
        if config['check_interval'] < 30:
            errors.append("监控间隔不能小于30秒")
        if config['max_pairs'] > 2000:
            errors.append("最大监控交易对数不能超过2000")
        for strategy in config['ma_strategies']:
            s, m, l = strategy['periods']
            if not (s < m < l):
                errors.append(f"无效的均线周期组合: {s}-{m}-{l}")
        if config['enable_tg'] and not (config['tg_token'] and config['tg_chat_id']):
            errors.append("启用Telegram需填写Token和Chat ID")
        # 验证代理格式
        if config['proxy'] and not config['proxy'].startswith(('http://', 'https://')):
            errors.append("代理地址需以 http:// 或 https:// 开头")
        # 验证价格阈值范围
        if not (0.1 <= config['price_threshold'] <= 50.0):
            errors.append("价格阈值需在0.1%~50%之间")
        return errors

    def load_config(self) -> None:
        """从配置文件加载配置，并填充到各控件"""
        default_config = {
            "exchanges": ["binance"],
            "proxy": "",
            "check_interval": 300,
            "max_pairs": 500,
            "enable_price_monitor": True,
            "price_timeframe": "5m",
            "price_threshold": 5.0,
            "price_direction": "both",
            "excluded_pairs": "",
            "enable_wechat": False,
            "wechat_webhook": "",
            "ma_strategies": [{
                "periods": [5, 20, 60],
                "timeframe": "1h"
            }],
            "data_retention": "6小时",
            "mem_interval": 600,
            "enable_tg": False,
            "tg_token": "",
            "tg_chat_id": "",
            "enable_bearish_ma":False,
            "enable_bullish_ma":False
        }
        config_path = "config.json"
        merged_config = default_config.copy()
        try:
            with open("config.json", "r", encoding="utf-8") as f:
                config = json.load(f)
                if not isinstance(config, dict):
                    print("⚠️ 配置文件格式错误，使用默认配置")
                    return {}
                return config
        except FileNotFoundError:
            print("⚠️ 未找到配置文件，使用默认配置")
            return {}
        except json.JSONDecodeError:
            print("⚠️ 配置文件格式错误，使用默认配置")
            return {}    
        try:
            if not os.path.exists(config_path):
                self.log_message("未找到配置文件，正在创建默认配置...", 'log')
                with open(config_path, "w", encoding='utf-8') as f:
                    json.dump(default_config, f, indent=2)
                self.log_message("已成功创建默认配置文件", 'log')
            else:
                with open(config_path, "r", encoding='utf-8') as f:
                    try:
                        user_config = json.load(f)
                        merged_config.update(user_config)
                        if 'exchanges' in user_config:
                            merged_config['exchanges'] = user_config['exchanges']
                        if 'ma_strategies' in user_config:
                            merged_config['ma_strategies'] = []
                            for strategy in user_config.get('ma_strategies', []):
                                if (isinstance(strategy, dict) and 'periods' in strategy and len(strategy['periods']) == 3):
                                    merged_config['ma_strategies'].append({
                                        'periods': strategy['periods'],
                                        'timeframe': strategy.get('timeframe', '1h')
                                    })
                    except json.JSONDecodeError:
                        self.log_message("配置文件格式错误，已重置为默认配置", 'warning')
                        with open(config_path, "w", encoding='utf-8') as f:
                            json.dump(default_config, f, indent=2)
                        merged_config = default_config.copy()
            # 将配置填充到控件中
            self.binance_var.set('binance' in merged_config['exchanges'])
            self.okx_var.set('okx' in merged_config['exchanges'])
            self.htx_var.set('htx' in merged_config['exchanges'])
            # 代理设置
            self.proxy_enable.set(merged_config.get('proxy_enable', True))
            if self.proxy_enable.get():
                self.proxy_entry.config(state=tk.NORMAL)
            else:
                self.proxy_entry.config(state=tk.DISABLED)
            self.proxy_entry.delete(0, tk.END)
            self.proxy_entry.insert(0, merged_config.get('proxy', 'http://127.0.0.1:10809'))

            self.price_enable.set(merged_config.get('enable_price_monitor', True))
            self.price_tf.set(merged_config.get('price_timeframe', '5m'))
            self.price_threshold.delete(0, tk.END)
            self.price_threshold.insert(0, str(merged_config.get('price_threshold', 5.0)))
            self.price_direction.set(merged_config.get('price_direction', 'both'))
            for i, strategy in enumerate(merged_config.get('ma_strategies', [])[:2]):
                if i < len(self.ma_configs):
                    cfg = self.ma_configs[i]
                    cfg['enable'].set(True)
                    periods = strategy.get('periods', [5, 20, 60])
                    for entry, value in zip([cfg['short'], cfg['medium'], cfg['long']], periods):
                        entry.delete(0, tk.END)
                        entry.insert(0, str(value))
                    cfg['timeframe'].set(strategy.get('timeframe', '1h'))
            self.data_retention.set(merged_config.get('data_retention', '6小时'))
            self.mem_interval.delete(0, tk.END)
            self.mem_interval.insert(0, str(merged_config.get('mem_interval', 600)))
            self.check_interval.delete(0, tk.END)
            self.check_interval.insert(0, str(merged_config.get('check_interval', 300)))
            self.tg_enable.set(merged_config.get('enable_tg', False))
            self.tg_token_entry.delete(0, tk.END)
            self.tg_token_entry.insert(0, merged_config.get('tg_token', ''))
            self.tg_chat_entry.delete(0, tk.END)
            self.tg_chat_entry.insert(0, merged_config.get('tg_chat_id', ''))

            # 填充排除交易对Listbox
            self.exclude_listbox.delete(0, tk.END)
            excluded_pairs = merged_config.get('excluded_pairs', '')
            if excluded_pairs:
                for pair in [x.strip().upper() for x in excluded_pairs.split(',') if x]:
                    self.exclude_listbox.insert(tk.END, pair)

            # 加载股票监控配置
            stock_list_str = merged_config.get('stock_list', '')
            self.stock_listbox.delete(0, tk.END)
            if stock_list_str:
                for code in [x.strip() for x in stock_list_str.split(',') if x]:
                    self.stock_listbox.insert(tk.END, code)
            self.stock_period.set(merged_config.get('stock_period', 'daily'))
            self.ma_period_entry.delete(0, tk.END)
            self.ma_period_entry.insert(0, str(merged_config.get('ma_period', 30)))
            self.ma_threshold_entry.delete(0, tk.END)
            self.ma_threshold_entry.insert(0, str(merged_config.get('ma_threshold', 2.0)))
            self.stock_interval_entry.delete(0, tk.END)
            self.stock_interval_entry.insert(0, str(merged_config.get('stock_check_interval', 60)))


        except PermissionError:
            self.log_message("无配置文件写入权限，使用默认配置", 'warning')
        except Exception as e:
            self.log_message(f"配置加载异常: {str(e)}", 'warning')

    def save_config(self):
        """将当前配置保存到配置文件"""
        try:
            config = self.get_config()
            with open("config.json", "w", encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            self.log_message("配置保存成功", 'log')
            # 更新加密货币监控配置
            if self.monitor and hasattr(self.monitor, "update_config"):
                self.monitor.update_config(config)
            # 更新单个交易对监控列表
            if self.monitor and hasattr(self.monitor, "update_single_pair_list"):
                new_list = list(self.single_pair_listbox.get(0, tk.END))
                self.monitor.update_single_pair_list(new_list)
            # 更新股票监控配置（包括股票代码列表）
            if self.stock_monitor:
                if hasattr(self.stock_monitor, "update_config"):
                    self.stock_monitor.update_config(config)
                if hasattr(self.stock_monitor, "update_stock_list"):
                    stock_list = list(self.stock_listbox.get(0, tk.END))
                    self.stock_monitor.update_stock_list(stock_list)
        except Exception as e:
            messagebox.showerror("保存错误", str(e))

    def start_monitor(self) -> None:
        """启动监控服务"""
        if self.is_monitoring:
            self.log_message("监控系统已经启动，无法重复启动。", 'warning')
            return
        try:
            config = self.get_config()

            self.monitor = CryptoMonitorPro(config, self.log_message)
            # 启动后台线程
            self.monitor_thread = Thread(target=self.monitor.start_monitoring, daemon=True)
            self.monitor_thread.start()

            # 更新状态标志
            self.is_monitoring = True
            self.start_btn.config(state=tk.DISABLED)
            self.stop_btn.config(state=tk.NORMAL)
            self.log_message("监控系统启动成功", 'log')
        except ValueError as e:
            messagebox.showerror("配置错误", str(e))
        except Exception as e:
            messagebox.showerror("启动失败", f"未知错误: {str(e)}")

    def stop_monitor(self) -> None:
        """停止监控服务"""
        if not self.is_monitoring:
            self.log_message("监控系统未启动", 'warning')
            return
        # 停止监控进程
        if self.monitor:
            self.monitor.running.clear()  # 停止运行监控进程
            self.monitor.optimizer.running = False  # 停止内存优化线程
            self.is_monitoring = False  # 更新状态
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.log_message("监控系统已停止", 'log')

    def on_close(self) -> None:
        """窗口关闭时停止监控并退出"""
        self.monitor.running.clear()
        if self.monitor.optimizer.is_alive():
            self.monitor.optimizer.stop()
        if messagebox.askokcancel("退出", "确定要退出程序吗？"):
            self.stop_monitor()
            self.destroy()

#===========
#==============================
# 定义 Windows API 常量
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040  # Windows Vista 及以上

def prevent_sleep():
    """
    调用 SetThreadExecutionState API，防止系统进入睡眠状态
    """
    result = ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED)
    if result == 0:
        print("调用 SetThreadExecutionState 失败")
    else:
        print("系统休眠已被禁止")

def restore_sleep():
    """
    恢复系统默认睡眠状态
    """
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    print("系统休眠已恢复")

# 在程序启动时调用 prevent_sleep，并确保程序退出时恢复睡眠
prevent_sleep()
atexit.register(restore_sleep)
#===========

# crypto.py —— 核心监控和 GUI 代码（包含 CryptoMonitorPro、MonitorGUIPro 等）
# …（你的所有类和函数代码）…
# 在 crypto.py 顶层定义（不要放在 if __name__ == '__main__': 块内）
def get_monitor():
    # 返回当前监控对象，例如你在 main 部分创建的 MonitorGUIPro 对象中的 monitor 属性
    return _global_monitor

#==============================
if __name__ == "__main__":
    import sys
    import multiprocessing
    import json

    if sys.platform == "win32":
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)  # 高DPI支持

    # 读取配置（示例代码，可根据实际情况调整）
    try:
        with open("config.json", "r", encoding="utf-8") as f:
            current_config = json.load(f)
    except Exception:
        current_config = {}

    # 创建 GUI 对象，并将 monitor 属性赋值给全局变量 _global_monitor
    app_gui = MonitorGUIPro()
    _global_monitor = app_gui.monitor  # 使得 get_monitor() 能够返回正确的监控对象

    # 启动 Web 服务器进程，注意使用 multiprocessing.Process
    from web_server import run_web_server  # web_server.py 文件中的 run_web_server 方法
    web_server_process = multiprocessing.Process(target=run_web_server, args=(get_monitor,))
    web_server_process.start()

    # 启动 GUI
    app_gui.mainloop()

    # 退出时关闭 Web 服务器进程
    web_server_process.terminate()
    web_server_process.join()


