# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Chinese fapiao (invoice) reimbursement automation tool (v3). Single-file Python 3 script that runs as a batch pipeline: download invoice PDFs from QQ Mail IMAP → parse/classify → select for reimbursement → ZIP and send via Feishu (Lark) messaging.

## Running

```bash
pip3 install pymupdf requests
python3 invoice_automation_v3.py
```

No build step, no tests, no linting configured.

## Architecture

The entire application is `invoice_automation_v3.py` (862 lines). Pipeline stages executed sequentially by `main()` (line 820):

1. **Download** (`download_invoices_from_email`, line 210) — Connects to QQ Mail IMAP, extracts PDF attachments, and downloads invoices from CDN links (huapiaoer, fapiaoer, bwjf/wangqiycloud)
2. **Classify & Store** (`classify_and_store_by_invoice_date`, line 481) — Parses PDF text with PyMuPDF, categorizes by date and type, stores in directory structure
3. **Deduplicate** (`process_invoices`, line 540) — Removes duplicates by MD5 hash and invoice number
4. **Select** (`select_invoices_for_reimbursement`, line 624) — Scans last 3 months, picks invoices per category up to threshold amounts
5. **Mark Used** (`mark_invoices_as_used`, line 672) — Moves selected PDFs from `未使用/` to `已使用/`
6. **Pack & Send** (`pack_and_send`, line 766) — Creates ZIP and sends to Feishu user via API

### Key call graph

```
main()
 ├─ download_invoices_from_email()
 │   └─ classify_and_store_by_invoice_date()
 │       └─ extract_invoice_info() → is_valid_invoice(), classify_invoice(), extract_invoice_date()
 ├─ process_invoices()
 ├─ select_invoices_for_reimbursement()
 │   └─ scan_invoices_from_directory() → extract_invoice_info()
 ├─ mark_invoices_as_used()
 └─ pack_and_send() → send_to_feishu()
```

## Configuration

Credentials are read from `/root/.openclaw/openclaw.json` (line 31):
- `config.env.QQMAIL_USER` / `QQMAIL_AUTH_CODE` — QQ Mail IMAP access
- `config.channels.feishu.appId` / `appSecret` — Feishu app credentials

Hardcoded paths:
- Invoice base directory: `/lhcos-data/obsidian-vault/发票` (line 33)
- Feishu recipient Open ID: `ou_126cd3096c23bedeaacce6f0fba1c621` (line 731)

## Invoice directory structure (on disk)

```
/lhcos-data/obsidian-vault/发票/
  YYYY-MM/                    # Monthly folders by invoice date
    未使用/                    # Unused invoices
      餐饮/ 交通/ 通信/ 其他/  # Category subdirs
    已使用/                    # Used (reimbursed) invoices
      餐饮/ 交通/ 通信/ 其他/
```

## Business rules

- **Reimbursement thresholds** (lines 36-40): 餐饮(dining) ¥680, 交通(transport) ¥500, 通信(telecom) ¥300
- **Invoice validation**: PDF must contain at least 2 of: 发票号码, 发票代码, 价税合计, 税率, 购买方, 销售方, 增值税
- **Classification**: Keyword matching against `INVOICE_KEYWORDS` dict (line 42) — dining keywords like 餐饮/饭店, transport like 出租车/加油, telecom like 通信/移动
- **Selection window**: Last 3 months from current date
- **Deduplication**: By MD5 file hash and 20-digit invoice number

## External services

| Service | Protocol | Purpose |
|---|---|---|
| QQ Mail | IMAP over SSL (`imap.qq.com:993`) | Download invoice emails |
| Huapiaoer CDN | HTTPS | Download invoice PDFs from email links |
| Fapiaoer CDN | HTTPS | Download invoice PDFs |
| bwjf.cn (Wangqiyun) | HTTPS API | Gas/fuel invoice retrieval |
| Feishu (Lark) | REST API | Upload ZIP and send notification message |
