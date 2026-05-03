#!/usr/bin/env python3
"""
发票自动化处理脚本 v3
"""

import os
import sys

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')
import json
import re
import zipfile
import hashlib
import tempfile
import shutil
import argparse
from datetime import datetime, timedelta
from collections import defaultdict

try:
    import fitz
except ImportError:
    print("错误: 请安装 pymupdf: pip3 install pymupdf")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("错误: 请安装 requests: pip install requests")
    sys.exit(1)

# ==================== 配置 ====================

OPENCLAW_CONFIG = "/root/.openclaw/openclaw.json"
OBSIDIAN_VAULT = "/lhcos-data/obsidian-vault"
INVOICE_BASE_DIR = os.path.join(OBSIDIAN_VAULT, "发票")
BATCH_FILE = os.path.join(INVOICE_BASE_DIR, "batches.json")

# 报销金额阈值
THRESHOLDS = {
    "餐饮": 680,
    "交通": 500,
    "通信": 300
}

# 发票分类关键词
INVOICE_KEYWORDS = {
    "餐饮": ["餐饮", "饭店", "餐厅", "食品", "酒楼", "火锅", "快餐", "外卖", "饮品", "咖啡", "茶", "餐费", "宴"],
    "交通": ["通行费", "高速公路", "路桥", "etc", "车辆", "客运", "货运", "机票", "火车", "打车", "网约车", "加油", "停车", "石化"],
    "通信": ["电信", "移动", "联通", "通信", "话费", "宽带", "流量", "手机"]
}

# 发票必须包含的特征
INVOICE_REQUIRED_FEATURES = ["发票号码", "发票代码", "价税合计", "税率", "购买方", "销售方", "增值税"]

# ==================== 日志 ====================

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {msg}")

# ==================== 工具函数 ====================

def get_previous_month():
    """获取上个月的年份和月份"""
    today = datetime.now()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_of_prev_month.year, last_of_prev_month.month

def _parse_date_groups(groups):
    """将正则匹配的日期组转换为 datetime"""
    if len(groups) == 3:
        date_str = f"{groups[0]}-{groups[1]}-{groups[2]}"
    else:
        date_str = groups[0].replace('年', '-').replace('月', '-').replace('日', '')
    date_str = date_str.replace(' ', '')
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except Exception:
        return None

def extract_invoice_date(text):
    """从发票内容中提取开票日期"""
    # 1. 开票日期和日期在同一行
    same_line_patterns = [
        r'开票日期[：:]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
        r'开票日期[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)',
        r'开票日期[：:]\s*(\d{4}-\d{2}-\d{2})',
    ]
    for pattern in same_line_patterns:
        match = re.search(pattern, text)
        if match:
            result = _parse_date_groups(match.groups())
            if result:
                return result

    # 2. 开票日期和日期跨行（PDF排版拆分）
    cross_line_patterns = [
        r'开票日期[：:]\s*\n\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
        r'开票日期[：:]\s*\n\s*(\d{4}年\d{1,2}月\d{1,2}日)',
        r'开票日期[：:]\s*\n\s*(\d{4}-\d{2}-\d{2})',
    ]
    for pattern in cross_line_patterns:
        match = re.search(pattern, text)
        if match:
            result = _parse_date_groups(match.groups())
            if result:
                return result

    # 3. 回退：查找开票日期之后最近的日期
    kp_match = re.search(r'开票日期[：:]', text)
    if kp_match:
        after = text[kp_match.end():]
        fallback_patterns = [
            r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日',
            r'(\d{4}-\d{2}-\d{2})',
        ]
        for pattern in fallback_patterns:
            m = re.search(pattern, after)
            if m:
                result = _parse_date_groups(m.groups())
                if result:
                    return result

    return None

def get_invoice_quarter():
    """返回最近3个月，从旧到新排列，如 [(2026,2), (2026,3), (2026,4)]"""
    today = datetime.now()
    months = []
    for i in range(1, 4):
        first_of_this_month = today.replace(day=1)
        for _ in range(i):
            first_of_this_month = (first_of_this_month - timedelta(days=1)).replace(day=1)
        months.append((first_of_this_month.year, first_of_this_month.month))
    months.reverse()
    return months

# ==================== 文件名安全处理 ====================

def is_safe_char(c):
    """判断字符是否为安全文件名字符（含中文）"""
    return c.isalnum() or c in ".-_" or '一' <= c <= '鿿'

def safe_filename(name):
    """将文件名中的不安全字符替换为下划线"""
    return "".join(c if is_safe_char(c) else "_" for c in name)

# ==================== Batch 管理 ====================

def load_batches():
    """读取 batches.json，不存在返回空结构"""
    if os.path.exists(BATCH_FILE):
        with open(BATCH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _fix_batch_paths(data)
        return data
    return {"batches": []}

def _fix_batch_paths(data):
    """修复老数据中 filepath == dest_path 的问题（将 filepath 修正为未使用/路径）"""
    changed = False
    for batch in data.get("batches", []):
        for cat, inv_list in batch.get("selected", {}).items():
            for inv in inv_list:
                dest = inv.get("dest_path", "")
                fp = inv.get("filepath", "")
                # filepath 和 dest_path 相同且包含 已使用 → 修正 filepath 为 未使用
                if dest and fp == dest and "已使用" in dest:
                    inv["filepath"] = dest.replace("已使用", "未使用")
                    changed = True
    if changed:
        save_batches(data)

def save_batches(data):
    """原子写入 batches.json"""
    os.makedirs(os.path.dirname(BATCH_FILE), exist_ok=True)
    tmp = BATCH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BATCH_FILE)

def get_used_invoice_set():
    """获取所有未取消 batch 中已使用的发票集合（invoice_nums, file_hashes）"""
    data = load_batches()
    invoice_nums = set()
    file_hashes = set()
    for batch in data["batches"]:
        if batch["status"] == "cancelled":
            continue
        for cat, inv_list in batch.get("selected", {}).items():
            for inv in inv_list:
                num = inv.get("invoice_num")
                h = inv.get("file_hash")
                if num:
                    invoice_nums.add(num)
                if h:
                    file_hashes.add(h)
    return invoice_nums, file_hashes

def add_batch(window, selected):
    """创建新 batch 记录，返回 batch_id"""
    data = load_batches()
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch = {
        "batch_id": batch_id,
        "created_at": datetime.now().isoformat(),
        "status": "active",
        "window": list(window),
        "selected": {}
    }
    for category, inv_list in selected.items():
        batch["selected"][category] = []
        for inv in inv_list:
            batch["selected"][category].append({
                "invoice_num": inv.get("invoice_num"),
                "filepath": inv.get("filepath", ""),
                "dest_path": inv.get("dest_path", ""),
                "file_hash": inv.get("file_hash", get_file_hash(inv.get("filepath", "")) if inv.get("filepath") and os.path.exists(inv.get("filepath", "")) else ""),
                "amount": inv.get("amount"),
                "date": inv.get("date").strftime("%Y-%m-%d") if inv.get("date") else None
            })
    data["batches"].append(batch)
    save_batches(data)
    log(f"已创建 batch: {batch_id}")
    return batch_id

def update_batch_status(batch_id, status):
    """更新 batch 状态"""
    data = load_batches()
    for batch in data["batches"]:
        if batch["batch_id"] == batch_id:
            batch["status"] = status
            save_batches(data)
            log(f"batch {batch_id} 状态更新为: {status}")
            return True
    log(f"未找到 batch: {batch_id}")
    return False

def _find_file_by_hash(directory, target_hash):
    """在目录下按 hash 查找文件"""
    if not os.path.isdir(directory):
        return None
    for f in os.listdir(directory):
        fp = os.path.join(directory, f)
        if not f.lower().endswith('.pdf') or not os.path.isfile(fp):
            continue
        try:
            if get_file_hash(fp) == target_hash:
                return fp
        except Exception:
            pass
    return None

def cancel_batch(batch_id, force=False):
    """取消 batch，将发票从已使用移回未使用"""
    data = load_batches()
    batch = None
    for b in data["batches"]:
        if b["batch_id"] == batch_id:
            batch = b
            break
    if not batch:
        log(f"未找到 batch: {batch_id}")
        return
    if batch["status"] == "cancelled" and not force:
        log(f"batch {batch_id} 已经是取消状态（使用 --force-cancel 强制重新恢复）")
        return

    restored = 0
    skipped = 0
    failed = 0
    for cat, inv_list in batch.get("selected", {}).items():
        for inv in inv_list:
            dest = inv.get("dest_path", "")
            orig = inv.get("filepath", "")
            target_hash = inv.get("file_hash", "")
            if not orig:
                continue

            # 确定源文件：优先 dest_path，其次按 hash 在已使用目录查找
            src = None
            if dest and os.path.exists(dest):
                src = dest
            elif dest and target_hash:
                # 文件可能被之前的 cancel 重命名，按 hash 查找
                dest_dir = os.path.dirname(dest)
                src = _find_file_by_hash(dest_dir, target_hash)
                if src:
                    log(f"  按 hash 找到文件: {os.path.basename(src)}")

            if not src:
                skipped += 1
                continue

            orig_dir = os.path.dirname(orig)
            os.makedirs(orig_dir, exist_ok=True)
            move_target = orig
            counter = 1
            while os.path.exists(move_target):
                base, ext = os.path.splitext(orig)
                move_target = f"{base}_{counter}{ext}"
                counter += 1
            try:
                shutil.move(src, move_target)
                restored += 1
                log(f"  恢复: {os.path.basename(src)} -> {os.path.dirname(orig)}")
            except Exception as e:
                failed += 1
                log(f"  恢复失败: {os.path.basename(src)} - {e}")

    batch["status"] = "cancelled"
    save_batches(data)
    log(f"batch {batch_id} 已取消，恢复 {restored} 张，跳过 {skipped} 张，失败 {failed} 张")

def list_batches():
    """列出所有 batch 记录"""
    data = load_batches()
    if not data["batches"]:
        log("没有 batch 记录")
        return
    for batch in data["batches"]:
        window_str = ", ".join(f"{y}-{m:02d}" for y, m in batch.get("window", []))
        total_inv = sum(len(v) for v in batch.get("selected", {}).values())
        total_amount = sum(
            inv.get("amount") or 0
            for cat_list in batch.get("selected", {}).values()
            for inv in cat_list
        )
        print(f"  [{batch['status']}] {batch['batch_id']}  "
              f"窗口: {window_str}  "
              f"发票: {total_inv}张  "
              f"金额: ¥{total_amount:.2f}  "
              f"创建: {batch.get('created_at', '?')}")

def resend_batch(batch_id):
    """重新打包发送指定 batch"""
    data = load_batches()
    batch = None
    for b in data["batches"]:
        if b["batch_id"] == batch_id:
            batch = b
            break
    if not batch:
        log(f"未找到 batch: {batch_id}")
        return

    selected = {}
    for cat, inv_list in batch.get("selected", {}).items():
        selected[cat] = []
        for inv in inv_list:
            path = inv.get("dest_path") or inv.get("filepath", "")
            if path and os.path.exists(path):
                selected[cat].append({"filepath": path, "amount": inv.get("amount")})

    if not any(selected.values()):
        log("batch 中的发票文件不存在，无法重发")
        return

    pack_and_save(selected, batch_id)

# ==================== 发票验证和分类 ====================

def is_valid_invoice(text):
    """严格验证是否是真正的发票"""
    # 必须包含至少2个发票特征
    feature_count = sum(1 for kw in INVOICE_REQUIRED_FEATURES if kw in text)
    if feature_count < 2:
        return False
    
    # 必须包含发票号码
    if "发票号码" not in text:
        return False
    
    # 检查是否有金额信息
    if not re.search(r'¥[\d,]+\.?\d*', text) and "价税合计" not in text:
        return False
    
    return True

def classify_invoice(text):
    """根据发票内容分类"""
    text_lower = text.lower()
    
    for category, keywords in INVOICE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower or kw in text:
                return category
    
    return "其他"

def extract_invoice_info(filepath):
    """提取发票信息（严格模式）"""
    try:
        with fitz.open(filepath) as doc:
            text = "".join(page.get_text() for page in doc)
        
        # 严格验证是否是发票
        if not is_valid_invoice(text):
            return None
        
        # 提取发票号码
        patterns = [
            r'发票号码[：:]\s*[‖|]?\s*(\d{20})',
            r'发票号码[：:]\s*[‖|]?\s*(\d+)',
        ]
        invoice_num = None
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                invoice_num = match.group(1)
                break
        
        # 提取金额
        amount = None
        clean_text = re.sub(r'\s+', ' ', text)
        
        amount_patterns = [
            r'价税合计[（(]小写[)）][：:]?\s*[¥￥]?\s*([\d,]+\.?\d*)',
            r'[¥￥]\s*([\d,]+\.\d{2})',
        ]
        
        for pattern in amount_patterns:
            match = re.search(pattern, clean_text)
            if match:
                try:
                    amount = float(match.group(1).replace(',', ''))
                    break
                except Exception:
                    pass

        # 特殊处理：有些发票格式是 "金额 ¥" 而不是 "¥ 金额"
        if amount is None:
            match = re.search(r'([\d,]+\.\d{2})\s*¥', clean_text)
            if match:
                try:
                    amount = float(match.group(1).replace(',', ''))
                except Exception:
                    pass
        
        # 提取开票日期
        invoice_date = extract_invoice_date(text)
        
        category = classify_invoice(text)
        
        return {
            "invoice_num": invoice_num,
            "amount": amount,
            "date": invoice_date,
            "category": category,
            "filepath": filepath
        }
    except Exception as e:
        return None

def get_file_hash(filepath):
    h = hashlib.md5()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()

# ==================== 下载发票 ====================

def download_invoices_from_email(year, month):
    """从邮箱下载指定月份收到的发票邮件，但按发票实际开票日期存放"""
    log(f"下载 {year}年{month}月 收到的发票邮件...")
    
    # 临时下载目录
    temp_dir = tempfile.mkdtemp()
    
    # 读取配置
    with open(OPENCLAW_CONFIG, "r") as f:
        config = json.load(f)
    
    env_config = config.get("env", {})
    user = env_config.get("QQMAIL_USER", "")
    auth_code = env_config.get("QQMAIL_AUTH_CODE", "")
    
    if not user or not auth_code:
        log("错误: QQ邮箱配置未找到")
        return []
    
    import imaplib
    import ssl
    import email
    from email.header import decode_header

    context = ssl.create_default_context()
    conn = imaplib.IMAP4_SSL("imap.qq.com", 993, ssl_context=context)
    conn.login(user, auth_code)

    try:
        conn.select("INBOX", readonly=True)

        month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        since_date = f"01-{month_names[month-1]}-{year}"

        if month == 12:
            next_year, next_month = year + 1, 1
        else:
            next_year, next_month = year, month + 1
        before_date = f"01-{month_names[next_month-1]}-{next_year}"

        status, messages = conn.search(None, "SINCE", since_date, "BEFORE", before_date)
        msg_ids = messages[0].split()

        log(f"找到 {len(msg_ids)} 封邮件")

        pending_classification = []

        for msg_id in msg_ids:
            try:
                status, data = conn.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(data[0][1])

                # 提取并打印邮件标题
                subject_raw = msg.get("Subject", "")
                subject_parts = decode_header(subject_raw)
                subject = "".join(
                    p.decode(c or "utf-8", errors="replace") if isinstance(p, bytes) else p
                    for p, c in subject_parts
                )
                log(f"处理邮件: {subject}")

                # 下载附件
                for part in msg.walk():
                    disposition = str(part.get("Content-Disposition", ""))
                    if "attachment" in disposition:
                        filename = part.get_filename()
                        if filename:
                            parts = decode_header(filename)
                            filename = "".join(
                                p.decode(c or "utf-8", errors="replace") if isinstance(p, bytes) else p
                                for p, c in parts
                            )

                            payload = part.get_payload(decode=True)
                            if payload:
                                safe_name = safe_filename(filename)
                                filepath = os.path.join(temp_dir, safe_name)

                                counter = 1
                                while os.path.exists(filepath):
                                    base, ext = os.path.splitext(safe_name)
                                    filepath = os.path.join(temp_dir, f"{base}_{counter}{ext}")
                                    counter += 1

                                with open(filepath, "wb") as f:
                                    f.write(payload)

                                if filename.lower().endswith('.zip'):
                                    log(f"  解压 ZIP: {safe_name}")
                                    try:
                                        with zipfile.ZipFile(filepath, 'r') as z:
                                            for name in z.namelist():
                                                if name.lower().endswith('.pdf'):
                                                    source = z.open(name)
                                                    pdf_path = os.path.join(temp_dir, os.path.basename(name))
                                                    with open(pdf_path, 'wb') as target:
                                                        target.write(source.read())
                                                    pending_classification.append(pdf_path)
                                        os.remove(filepath)
                                    except Exception as e:
                                        log(f"    解压失败: {e}")
                                else:
                                    pending_classification.append(filepath)
                                    log(f"  下载附件: {safe_name}")

                # 获取邮件正文，查找票慧通等链接
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/html":
                            payload = part.get_payload(decode=True)
                            if payload:
                                charset = part.get_content_charset() or "utf-8"
                                body = payload.decode(charset, errors="replace")
                                break

                # 从正文中提取下载链接（票慧通、旺企云等）
                cdn_patterns = [
                    r'(https://cdn\.huapiaoer\.com/[^\s<>"\']+\.pdf[^\s<>"\']*)',
                    r'(https://upload\.fapiaoer\.cn/[^\s<>"\']+\.pdf[^\s<>"\']*)',
                    r'(https://[^\s<>"\']*fapiaoer\.[^\s<>"\']+\.pdf[^\s<>"\']*)'
                ]

                for pattern in cdn_patterns:
                    cdn_links = re.findall(pattern, body, re.IGNORECASE)
                    for link in cdn_links:
                        try:
                            filename = link.split('/')[-1]
                            if not filename.endswith('.pdf'):
                                filename += '.pdf'

                            filepath = os.path.join(temp_dir, filename)

                            if os.path.exists(filepath):
                                continue

                            resp = requests.get(link, timeout=30, headers={
                                "User-Agent": "Mozilla/5.0"
                            })

                            if resp.status_code == 200:
                                with open(filepath, "wb") as f:
                                    f.write(resp.content)
                                pending_classification.append(filepath)
                                log(f"  下载链接: {filename}")
                        except Exception as e:
                            log(f"  下载链接失败: {e}")

                # 旺企云发票链接（加油发票等）
                bwjf_patterns = [
                    r'(https://www\.bwjf\.cn/allEleInvoiceSmsLink\?[^"\'>\s]+)',
                    r'(https://www\.bwjf\.cn/allEleInvoiceSmsResult\?[^"\'>\s]+)'
                ]

                for pattern in bwjf_patterns:
                    bwjf_links = re.findall(pattern, body)
                    for bwjf_link in bwjf_links:
                        try:
                            from urllib.parse import urlparse, parse_qs, unquote

                            parsed = urlparse(bwjf_link)
                            params = parse_qs(parsed.query)

                            if 'allEleInvoiceSmsResult' in bwjf_link:
                                serial_number = params.get('serialNumber', [''])[0]
                                log(f"  旺企云发票 (smsResult): serial={serial_number}")

                                api_url = f"https://www.bwjf.cn/api/eleInvoice/getBySerial?serialNumber={serial_number}"
                                try:
                                    resp = requests.get(api_url, timeout=30, headers={
                                        "User-Agent": "Mozilla/5.0",
                                        "Referer": "https://www.bwjf.cn/"
                                    })
                                    if resp.status_code == 200:
                                        data = resp.json()
                                        if data.get('code') == 0:
                                            invoice_data = data.get('data', {})
                                            pdf_url = invoice_data.get('pdfUrl', '')
                                            fphm = invoice_data.get('fphm', '未知')
                                            jshj = invoice_data.get('jshj', '0')
                                            xsfmc = invoice_data.get('xsfmc', '未知')

                                            if pdf_url:
                                                filename = f"{fphm}_{jshj}_{xsfmc}.pdf"
                                                filename = safe_filename(filename)
                                                filepath = os.path.join(temp_dir, filename)

                                                pdf_resp = requests.get(pdf_url, timeout=30, headers={"User-Agent": "Mozilla/5.0"}, verify=False)  # TODO: 安装旺企云CA证书后移除 verify=False
                                                if pdf_resp.status_code == 200 and len(pdf_resp.content) > 1000:
                                                    with open(filepath, "wb") as f:
                                                        f.write(pdf_resp.content)
                                                    pending_classification.append(filepath)
                                                    log(f"    下载成功: {filename}")
                                                    continue
                                except Exception as e:
                                    log(f"    API 获取失败: {e}")

                            else:
                                pdf_url = unquote(params.get('pdfUrl', [''])[0])
                                fphm = params.get('fphm', ['未知'])[0]
                                jshj = params.get('jshj', ['0'])[0]
                                xsfmc = unquote(params.get('xsfmc', ['未知'])[0])

                                if not pdf_url:
                                    continue

                                log(f"  旺企云发票 (smsLink): {xsfmc} ¥{jshj}")

                                filename = f"{fphm}_{jshj}_{xsfmc}.pdf"
                                filename = safe_filename(filename)
                                filepath = os.path.join(temp_dir, filename)

                                if os.path.exists(filepath):
                                    continue

                                try:
                                    resp = requests.get(pdf_url, timeout=30, headers={
                                        "User-Agent": "Mozilla/5.0"
                                    }, verify=False)

                                    if resp.status_code == 200 and len(resp.content) > 1000:
                                        with open(filepath, "wb") as f:
                                            f.write(resp.content)
                                        pending_classification.append(filepath)
                                        log(f"    下载成功: {filename}")
                                        continue
                                except Exception:
                                    pass

                            log(f"    无法自动下载，记录到待处理列表")

                            pending_file = os.path.join(INVOICE_BASE_DIR, "旺企云待下载发票.txt")
                            with open(pending_file, 'a', encoding='utf-8') as f:
                                f.write(f"\n# {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                                f.write(f"链接: {bwjf_link}\n")
                                if 'fphm' in params:
                                    f.write(f"发票号码: {params.get('fphm', [''])[0]}\n")
                                if 'jshj' in params:
                                    f.write(f"金额: ¥{params.get('jshj', [''])[0]}\n")
                                if 'xsfmc' in params:
                                    f.write(f"销售方: {unquote(params.get('xsfmc', [''])[0])}\n")
                                if 'pdfUrl' in params:
                                    f.write(f"PDF直链: {unquote(params.get('pdfUrl', [''])[0])}\n")

                        except Exception as e:
                            log(f"  处理旺企云链接失败: {e}")

            except Exception as e:
                log(f"处理邮件失败: {e}")

    finally:
        try:
            conn.logout()
        except Exception:
            pass

    log(f"下载完成，共 {len(pending_classification)} 个文件")

    # 根据发票开票日期分类存放
    classified = classify_and_store_by_invoice_date(pending_classification)

    # 清理临时目录
    shutil.rmtree(temp_dir, ignore_errors=True)

    return classified

def _build_existing_hash_index(month_dir):
    """扫描月份目录下所有已有 PDF 的 hash 和发票号码，返回 (hashes, invoice_nums)"""
    hashes = set()
    invoice_nums = set()
    for subdir in ["未使用", "已使用"]:
        base = os.path.join(month_dir, subdir)
        if not os.path.exists(base):
            continue
        for category in os.listdir(base):
            cat_dir = os.path.join(base, category)
            if not os.path.isdir(cat_dir):
                continue
            for filename in os.listdir(cat_dir):
                if not filename.lower().endswith('.pdf'):
                    continue
                fp = os.path.join(cat_dir, filename)
                try:
                    hashes.add(get_file_hash(fp))
                    info = extract_invoice_info(fp)
                    if info and info.get("invoice_num"):
                        invoice_nums.add(info["invoice_num"])
                except Exception:
                    pass
    return hashes, invoice_nums

def classify_and_store_by_invoice_date(file_list):
    """根据发票实际开票日期分类存放，跨次全局去重"""
    log("根据发票开票日期分类存放...")

    classified = []
    used_invoice_nums, used_file_hashes = get_used_invoice_set()

    # 预构建各月份已有文件的 hash 索引，避免 O(n^2)
    month_hash_cache = {}

    for filepath in file_list:
        try:
            file_hash = get_file_hash(filepath)

            # 检查文件哈希是否已在已用集合中
            if file_hash in used_file_hashes:
                log(f"  文件已使用，跳过: {os.path.basename(filepath)}")
                os.remove(filepath)
                continue

            # 提取发票信息
            info = extract_invoice_info(filepath)

            if not info:
                log(f"  非发票文件，跳过: {os.path.basename(filepath)}")
                os.remove(filepath)
                continue

            # 检查发票号码是否已在已用集合中
            invoice_num = info.get("invoice_num")
            if invoice_num and invoice_num in used_invoice_nums:
                log(f"  发票已使用，跳过: {os.path.basename(filepath)} (号码: {invoice_num})")
                os.remove(filepath)
                continue

            # 获取发票开票日期
            invoice_date = info.get("date")

            if invoice_date:
                year = invoice_date.year
                month = invoice_date.month
                month_dir = os.path.join(INVOICE_BASE_DIR, f"{year}-{month:02d}")
            else:
                log(f"  无法获取开票日期，使用当前月份: {os.path.basename(filepath)}")
                year, month = datetime.now().year, datetime.now().month
                month_dir = os.path.join(INVOICE_BASE_DIR, f"{year}-{month:02d}")

            # 用缓存查 hash + 发票号码去重
            month_key = f"{year}-{month:02d}"
            if month_key not in month_hash_cache:
                month_hash_cache[month_key] = _build_existing_hash_index(month_dir)
            existing_hashes, existing_nums = month_hash_cache[month_key]
            if file_hash in existing_hashes:
                log(f"  文件已存在，跳过: {os.path.basename(filepath)}")
                os.remove(filepath)
                continue
            if invoice_num and invoice_num in existing_nums:
                log(f"  发票号码已存在，跳过: {os.path.basename(filepath)} (号码: {invoice_num})")
                os.remove(filepath)
                continue

            category = info.get("category", "其他")
            unused_dir = os.path.join(month_dir, "未使用", category)
            os.makedirs(unused_dir, exist_ok=True)

            # 移动文件到未使用目录
            dest_path = os.path.join(unused_dir, os.path.basename(filepath))

            counter = 1
            while os.path.exists(dest_path):
                base, ext = os.path.splitext(os.path.basename(filepath))
                dest_path = os.path.join(unused_dir, f"{base}_{counter}{ext}")
                counter += 1

            shutil.move(filepath, dest_path)
            info["filepath"] = dest_path
            info["file_hash"] = file_hash
            classified.append(info)

            # 更新缓存
            existing_hashes.add(file_hash)
            if invoice_num:
                existing_nums.add(invoice_num)

            log(f"  [{category}] {os.path.basename(dest_path)} -> {year}-{month:02d}")

        except Exception as e:
            log(f"  分类失败: {os.path.basename(filepath)} - {e}")

    return classified

# ==================== 处理发票 ====================

def process_invoices(classified_invoices):
    """处理发票：解压、验证、分类、去重（已下载的发票已经按日期分类存放）"""
    log("处理已下载的发票...")
    
    # 按月份分组处理
    by_month = defaultdict(list)
    for inv in classified_invoices:
        filepath = inv.get("filepath", "")
        match = re.search(r'/(\d{4}-\d{2})/', filepath)
        if match:
            by_month[match.group(1)].append(inv)
    
    processed = []
    
    for month_key, invoices in by_month.items():
        month_dir = os.path.join(INVOICE_BASE_DIR, month_key)
        unused_dir = os.path.join(month_dir, "未使用")
        
        log(f"处理 {month_key}: {len(invoices)} 张发票")
        
        # 处理该月份的发票
        seen_hashes = {}
        seen_numbers = {}

        for inv in invoices:
            filepath = inv.get("filepath", "")
            if not os.path.exists(filepath):
                continue
            
            # 检查文件哈希
            file_hash = get_file_hash(filepath)
            if file_hash in seen_hashes:
                log(f"  重复文件，删除: {os.path.basename(filepath)}")
                os.remove(filepath)
                continue
            
            # 检查发票号码
            invoice_num = inv.get("invoice_num")
            if invoice_num and invoice_num in seen_numbers:
                log(f"  重复发票，删除: {os.path.basename(filepath)}")
                os.remove(filepath)
                continue
            
            seen_hashes[file_hash] = filepath
            if invoice_num:
                seen_numbers[invoice_num] = filepath
            
            processed.append(inv)
            log(f"  [{inv.get('category')}] {os.path.basename(filepath)} - ¥{inv.get('amount') or '?'}")
    
    log(f"处理完成：有效发票 {len(processed)} 张")
    return processed

# ==================== 筛选报销发票 ====================

def scan_invoices_from_directory(month_dir, used_set=None):
    """从目录结构扫描未使用发票，排除已使用的"""
    unused_dir = os.path.join(month_dir, "未使用")

    if not os.path.exists(unused_dir):
        return []

    if used_set is None:
        used_set = (set(), set())

    used_invoice_nums, used_file_hashes = used_set
    invoices = []

    for category in ["餐饮", "交通", "通信", "其他"]:
        cat_dir = os.path.join(unused_dir, category)
        if not os.path.exists(cat_dir):
            continue

        for filename in os.listdir(cat_dir):
            if not filename.lower().endswith('.pdf'):
                continue

            filepath = os.path.join(cat_dir, filename)

            # 按文件哈希排除
            try:
                file_hash = get_file_hash(filepath)
                if file_hash in used_file_hashes:
                    continue
            except Exception:
                continue

            info = extract_invoice_info(filepath)
            if not info:
                continue

            # 按发票号码排除
            if info.get("invoice_num") and info["invoice_num"] in used_invoice_nums:
                continue

            info["file_hash"] = file_hash
            invoices.append(info)

    return invoices

def _select_minimal_excess(invoices, target):
    """从发票列表中选择子集，使累加金额 >= target 且超出最小"""
    valid = [inv for inv in invoices if inv.get("amount") is not None]
    if not valid:
        return []

    n = len(valid)

    if n <= 20:
        # 穷举所有子集，找超出阈值最小的组合（同超出则选张数少的）
        amounts = [inv["amount"] for inv in valid]
        best_mask = None
        best_excess = float('inf')
        best_count = n + 1

        for mask in range(1, 1 << n):
            total = 0
            count = 0
            for j in range(n):
                if mask & (1 << j):
                    total += amounts[j]
                    count += 1
            if total >= target:
                excess = total - target
                if excess < best_excess or (excess == best_excess and count < best_count):
                    best_excess = excess
                    best_count = count
                    best_mask = mask

        if best_mask is not None:
            return [valid[j] for j in range(n) if best_mask & (1 << j)]
        return valid

    # 超过20张时贪心：按金额从大到小累加到阈值即停
    sorted_inv = sorted(valid, key=lambda x: x["amount"], reverse=True)
    total = 0
    selected = []
    for inv in sorted_inv:
        selected.append(inv)
        total += inv["amount"]
        if total >= target:
            break
    return selected

def _inv_label(inv):
    """生成发票简短标签用于日志"""
    name = os.path.basename(inv.get("filepath", "?"))
    amount = inv.get("amount")
    date = inv.get("date")
    date_str = date.strftime("%m-%d") if date else "无日期"
    return f"{name} ¥{amount:.2f} ({date_str})" if amount else f"{name} (无金额)"

def select_invoices_for_reimbursement():
    """筛选用于报销的发票（排除已使用，记录batch）

    策略：
    1. 最旧月份：按日期从旧到新累加到阈值即停（优先消耗即将过期的发票）
    2. 其余月份合并：贪心选出累计金额 >= 剩余阈值且超出最小的组合
    """
    log("开始筛选报销发票...")

    months = get_invoice_quarter()
    used_set = get_used_invoice_set()

    # 按月份、类别收集发票
    month_invoices = {}
    for year, month in months:
        month_dir = os.path.join(INVOICE_BASE_DIR, f"{year}-{month:02d}")
        if not os.path.exists(month_dir):
            continue
        invoices = scan_invoices_from_directory(month_dir, used_set)
        by_cat = {}
        for inv in invoices:
            cat = inv.get("category", "其他")
            if cat not in THRESHOLDS:
                continue
            by_cat.setdefault(cat, []).append(inv)
        if by_cat:
            month_invoices[(year, month)] = by_cat

    result = {}
    for category in THRESHOLDS:
        threshold = THRESHOLDS[category]
        log(f"--- {category} (阈值 ¥{threshold}) ---")

        # 找到有发票的月份（按时间从旧到新）
        available_months = [m for m in months
                            if m in month_invoices and category in month_invoices[m]
                            and any(inv.get("amount") is not None
                                   for inv in month_invoices[m][category])]

        if not available_months:
            log(f"  无可用发票，跳过")
            continue

        # 统计各月份数量
        for m in available_months:
            invs = [i for i in month_invoices[m][category] if i.get("amount") is not None]
            total = sum(i["amount"] for i in invs)
            m_str = f"{m[0]}-{m[1]:02d}"
            log(f"  [{m_str}] {len(invs)} 张可用，合计 ¥{total:.2f}")
            for inv in invs:
                log(f"    - {_inv_label(inv)}")

        selected_inv = []

        # 阶段1：最旧月份，按日期从旧到新累加到阈值
        oldest = available_months[0]
        oldest_str = f"{oldest[0]}-{oldest[1]:02d}"
        oldest_invs = [inv for inv in month_invoices[oldest][category]
                       if inv.get("amount") is not None]
        oldest_invs.sort(key=lambda x: x.get("date") or datetime.max)

        log(f"  [{oldest_str}] 最旧月份，按日期从旧到新累加：")
        picked_total = 0
        for inv in oldest_invs:
            selected_inv.append(inv)
            picked_total += inv["amount"]
            log(f"    + {_inv_label(inv)}  累计 ¥{picked_total:.2f}")
            if picked_total >= threshold:
                break

        if picked_total >= threshold:
            excess = picked_total - threshold
            log(f"  [{oldest_str}] 已达阈值，选中 {len(selected_inv)} 张 "
                f"¥{picked_total:.2f}，超出 ¥{excess:.2f}")
        else:
            log(f"  [{oldest_str}] 累计 ¥{picked_total:.2f}，未达阈值")

            # 阶段2：其余月份合并，贪心选最小超出
            remaining = threshold - picked_total
            newer_invs = []
            for m in available_months[1:]:
                for inv in month_invoices[m][category]:
                    if inv.get("amount") is not None:
                        newer_invs.append(inv)

            if newer_invs:
                newer_months_str = ", ".join(
                    f"{m[0]}-{m[1]:02d}" for m in available_months[1:]
                )
                log(f"  [{newer_months_str}] 合并贪心选补（需补 ¥{remaining:.2f}）：")
                picked = _select_minimal_excess(newer_invs, remaining)
                if picked:
                    for inv in picked:
                        selected_inv.append(inv)
                        log(f"    + {_inv_label(inv)}")
                    picked2_total = sum(inv["amount"] for inv in picked)
                    excess = picked2_total - remaining
                    log(f"  补选 {len(picked)} 张 ¥{picked2_total:.2f}，超出 ¥{excess:.2f}")
                else:
                    log(f"  无发票可补")
            else:
                log(f"  无其余月份发票可补")

        total = sum(inv["amount"] for inv in selected_inv)
        result[category] = selected_inv
        log(f"  => {category} 最终: {len(selected_inv)} 张，合计 ¥{total:.2f} (阈值 ¥{threshold})")

    return result, months

def mark_invoices_as_used(invoices_by_category):
    """将已使用的发票移动到已使用目录，并记录dest_path供回滚"""
    log("将已使用发票移动到已使用目录...")

    moved_count = 0

    for category, inv_list in invoices_by_category.items():
        for inv in inv_list:
            src_path = inv.get("filepath", "")
            if not src_path or not os.path.exists(src_path):
                continue

            # 从路径提取月份
            match = re.search(r'/(\d{4}-\d{2})/', src_path)
            if not match:
                continue

            month_key = match.group(1)
            month_dir = os.path.join(INVOICE_BASE_DIR, month_key)
            used_dir = os.path.join(month_dir, "已使用", category)
            os.makedirs(used_dir, exist_ok=True)

            # 移动文件
            dst_path = os.path.join(used_dir, os.path.basename(src_path))

            counter = 1
            while os.path.exists(dst_path):
                base, ext = os.path.splitext(os.path.basename(src_path))
                dst_path = os.path.join(used_dir, f"{base}_{counter}{ext}")
                counter += 1

            shutil.move(src_path, dst_path)
            inv["dest_path"] = dst_path
            moved_count += 1

    log(f"已移动 {moved_count} 张发票到已使用目录")

# ==================== 整理发票 ====================

def reorganize_month(month_str):
    """将指定月份下的未使用发票按实际开票日期重新归档到正确月份"""
    month_dir = os.path.join(INVOICE_BASE_DIR, month_str)
    unused_dir = os.path.join(month_dir, "未使用")
    if not os.path.exists(unused_dir):
        log(f"目录不存在: {unused_dir}")
        return

    moved = 0
    skipped = 0
    failed = 0

    for category in os.listdir(unused_dir):
        cat_dir = os.path.join(unused_dir, category)
        if not os.path.isdir(cat_dir):
            continue

        for filename in os.listdir(cat_dir):
            if not filename.lower().endswith('.pdf'):
                continue

            src_path = os.path.join(cat_dir, filename)
            info = extract_invoice_info(src_path)
            if not info or not info.get("date"):
                skipped += 1
                log(f"  跳过（无法提取日期）: {category}/{filename}")
                continue

            inv_date = info["date"]
            inv_month_str = f"{inv_date.year}-{inv_date.month:02d}"

            if inv_month_str == month_str:
                skipped += 1
                continue

            # 移动到正确月份
            inv_category = info.get("category", category)
            dest_dir = os.path.join(INVOICE_BASE_DIR, inv_month_str, "未使用", inv_category)
            os.makedirs(dest_dir, exist_ok=True)

            dest_path = os.path.join(dest_dir, filename)
            counter = 1
            while os.path.exists(dest_path):
                base, ext = os.path.splitext(filename)
                dest_path = os.path.join(dest_dir, f"{base}_{counter}{ext}")
                counter += 1

            try:
                shutil.move(src_path, dest_path)
                moved += 1
                log(f"  移动: {category}/{filename} -> {inv_month_str}/{inv_category}/")
            except Exception as e:
                failed += 1
                log(f"  移动失败: {category}/{filename} - {e}")

    log(f"整理完成: 移动 {moved} 张，跳过 {skipped} 张，失败 {failed} 张")

# ==================== 打包 ====================

def pack_and_save(invoices_by_category, batch_id=None):
    """打包发票保存到发票目录，更新batch状态"""
    log("打包发票...")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    temp_dir = tempfile.mkdtemp()

    try:
        pack_dir = os.path.join(temp_dir, f"报销发票_{timestamp}")
        os.makedirs(pack_dir)

        for category, inv_list in invoices_by_category.items():
            if not inv_list:
                continue

            cat_dir = os.path.join(pack_dir, category)
            os.makedirs(cat_dir)

            for inv in inv_list:
                src = inv.get("dest_path") or inv.get("filepath")
                if src and os.path.exists(src):
                    shutil.copy2(src, os.path.join(cat_dir, os.path.basename(src)))

        # 保存到发票目录下
        zip_filename = f"报销发票_{timestamp}.zip"
        zip_path = os.path.join(INVOICE_BASE_DIR, zip_filename)

        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for root, dirs, files in os.walk(pack_dir):
                for f in files:
                    filepath = os.path.join(root, f)
                    arcname = os.path.relpath(filepath, pack_dir)
                    z.write(filepath, arcname)

        # 生成摘要
        summary = []
        for cat, inv_list in invoices_by_category.items():
            total = sum(inv.get("amount") or 0 for inv in inv_list)
            summary.append(f"{cat}: {len(inv_list)}张, ¥{total:.2f}")

        log(f"打包完成: {zip_path}")
        log(f"报销包摘要:")
        for s in summary:
            log(f"  {s}")

        if batch_id:
            update_batch_status(batch_id, "sent")

        return zip_path

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

# ==================== 命令行参数 ====================

USAGE = """\
发票自动化处理脚本 (v3)

用法:
  python3 invoice_automation_v4.py                        默认运行完整流程（下载→分类→筛选→打包）
  python3 invoice_automation_v4.py --help                 查看所有命令说明
  python3 invoice_automation_v4.py --list-batches         列出所有批次记录（状态、金额、发票数）
  python3 invoice_automation_v4.py --cancel-batch ID      取消指定批次，将发票从已使用移回未使用
  python3 invoice_automation_v4.py --force-cancel ID      强制取消已取消的批次，重新恢复发票
  python3 invoice_automation_v4.py --resend ID            重新打包发送指定批次的发票
  python3 invoice_automation_v4.py --reorganize YYYY-MM   将指定月份未使用发票按实际开票日期重新归档

完整流程说明:
  1. 从QQ邮箱下载上月收到的发票邮件，按发票开票日期分类存放
  2. 去重（文件哈希+发票号码），过滤非发票文件
  3. 筛选报销发票：最旧月份全选（防止滑出窗口），次新月份按最小超出阈值补选
  4. 将选中发票从未使用目录移到已使用目录
  5. 记录批次（batches.json），支持取消回滚和重发
  6. 打包为ZIP保存到发票目录

报销阈值:
  餐饮 ¥680 | 交通 ¥500 | 通信 ¥300
"""

def parse_args():
    parser = argparse.ArgumentParser(
        description="发票自动化处理脚本 (v3)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=USAGE
    )
    parser.add_argument("--list-batches", action="store_true",
                        help="列出所有批次记录，显示状态、发票张数、金额等")
    parser.add_argument("--cancel-batch", metavar="BATCH_ID",
                        help="取消指定批次，将已使用发票移回未使用目录（仅限 active 状态）")
    parser.add_argument("--force-cancel", metavar="BATCH_ID",
                        help="强制取消已取消的批次，重新将发票从已使用移回未使用目录")
    parser.add_argument("--resend", metavar="BATCH_ID",
                        help="重新打包发送指定批次的发票（不改变批次状态）")
    parser.add_argument("--reorganize", metavar="YYYY-MM",
                        help="扫描指定月份的未使用发票，按发票实际开票日期移动到正确月份目录")
    return parser.parse_args()

# ==================== 主流程 ====================

def main():
    args = parse_args()

    if args.list_batches:
        list_batches()
        return
    if args.cancel_batch:
        cancel_batch(args.cancel_batch)
        return
    if args.force_cancel:
        cancel_batch(args.force_cancel, force=True)
        return
    if args.resend:
        resend_batch(args.resend)
        return
    if args.reorganize:
        reorganize_month(args.reorganize)
        return

    log("="*60)
    log("发票自动化处理任务开始 (v3)")
    log("="*60)

    try:
        # 1. 下载上个月收到的发票邮件，按发票实际开票日期存放（跨次去重）
        year, month = get_previous_month()
        classified = download_invoices_from_email(year, month)

        # 2. 处理发票（去重、验证）
        invoice_data = process_invoices(classified)

        # 3. 筛选报销发票（排除已使用）
        selected, months = select_invoices_for_reimbursement()

        if not any(selected.values()):
            log("没有符合条件的发票")
            return

        # 4. 标记已使用（移动文件，记录dest_path）
        mark_invoices_as_used(selected)

        # 5. 创建 batch 记录（此时 dest_path 已确定）
        batch_id = add_batch(months, selected)

        # 6. 打包发送，成功后更新 batch 状态为 sent
        pack_and_save(selected, batch_id)

        log("="*60)
        log("发票自动化处理任务完成")
        log("="*60)

    except Exception as e:
        log(f"任务执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
