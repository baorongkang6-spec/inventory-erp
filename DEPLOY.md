# 部署与上线清单（DEPLOY）

> 本文件记录本 ERP 的生产部署方式与安全要求。**开发阶段就要按"将来要走公网"来设计**，避免上线返工。

---

## 1. 部署形态（已确认）

- **生产主机**：公司一台 **Windows** 主机（开发在 Mac，跨平台，上线前需在 Windows 实测一遍）。
- **数据库**：代码支持 PostgreSQL 与 SQLite，靠 `DB_ENGINE` 环境变量切换。**实际生产部署用的是 SQLite**（数据量小、够用、免装 PG）；下文 PostgreSQL 相关步骤为可选的将来迁移方案。
- **WSGI 服务器**：Windows 上用 **Waitress**（⚠️ gunicorn 在 Windows 跑不了）。
- **外网访问**：公司服务器已装 **花生壳（Oray）做内网穿透 + 动态域名(DDNS)**，让手机/电脑在外网也能访问。
- **自启动**：用 **NSSM** 把 ERP 注册成 Windows 服务（开机自启、崩溃自重启）。

```
外网手机/电脑  →  花生壳域名(HTTPS)  →  穿透  →  公司内网 Windows 主机 (Waitress→Django) → PostgreSQL
```

---

## 2. 🔴 安全要求（因暴露公网 + 财务数据，必须做到）

> 一旦走花生壳暴露到公网，全网都能访问登录页。以下为**硬性要求**，开发期就要内置：

1. **强制 HTTPS**：对外必须加密（密码不能明文传输）。花生壳付费版绑域名 + SSL。
   - Django 生产开 `SECURE_SSL_REDIRECT`、`SESSION_COOKIE_SECURE`、`CSRF_COOKIE_SECURE`、`SECURE_HSTS_SECONDS`。
2. **`ALLOWED_HOSTS` / `CSRF_TRUSTED_ORIGINS`** 必须包含花生壳域名（按环境变量注入，别写死）。
3. **登录防爆破**：连续失败 N 次锁定一段时间（如 django-axes 或自实现）。
4. **强密码策略**：禁用演示弱密码（`erp12345` 仅开发用）；上线前全部改强密码。
5. **`DEBUG=False`、`SECRET_KEY` 走环境变量**，不进代码库。
6. **审计**：`AuditLog` 记录登录与关键操作，定期巡检异常登录。
7. 可选增强：访问白名单 / VPN（若安全要求更高，可让系统完全不暴露公网，改走 VPN 进内网）。

---

## 3. 生产主机硬件建议

| 项 | 推荐 |
|---|---|
| CPU / 内存 | 四核 i5 以上 / 16 GB |
| 硬盘 | 512 GB **SSD**（必须固态） |
| 系统 | Windows 10 / 11 64 位（专机专用，关自动重启更新） |
| 网络 | **有线**接路由器，设**固定内网 IP** |
| 电源 | 配 **UPS 不间断电源**（防停电损坏数据库） |
| 备份盘 | 一块移动硬盘/U盘存每日备份 |

---

## 4. 上线步骤（待系统就绪后执行，届时逐步带做）

1. Windows 装 Python 3.13、PostgreSQL、uv。
2. 拉取代码，`uv sync` 装依赖。
3. 复制 `.env.example` 为 `.env` 并改值（关键：`DJANGO_PRODUCTION=1`、`DJANGO_DEBUG=0`、
   `DJANGO_SECRET_KEY=`<50+位随机串>、`DJANGO_ALLOWED_HOSTS=`花生壳域名,内网IP、
   `DJANGO_CSRF_TRUSTED_ORIGINS=https://`花生壳域名、`DB_ENGINE=postgres` 及 DB_* 账号）。
   - 缺密钥/域名或 DEBUG 没关时程序会**启动即报错**（fail-fast），按提示补。
4. 初始化：
   ```
   uv run python manage.py migrate
   uv run python manage.py collectstatic --noinput
   uv run python manage.py seed_init           # 建三公司+角色（不带 --demo）
   uv run python manage.py createsuperuser      # 建管理员
   # 期初：登录后「报表▸期初导入」按模板上传，或交付时协助
   ```
5. 启动（Waitress，gunicorn 在 Windows 不可用）：
   ```
   uv run waitress-serve --listen=0.0.0.0:8000 config.wsgi:application
   ```
   用 **NSSM** 把上面这条注册为 Windows 服务（开机自启、崩溃重启），并在服务里注入 .env 环境变量。
6. 花生壳映射内网 IP:8000 → 绑域名 + 开 HTTPS（HTTPS 由花生壳终止，X-Forwarded-Proto 已在 settings 处理）。
7. **每日自动备份**：`pg_dump` 定时任务 + 异地拷贝（见 §5）。
8. 自检：`uv run python manage.py check --deploy` 应无高危告警；内外网各测一遍（含手机）；
   把演示/弱密码账号全部改强密码或删除 → 交付。

---

## 5. 备份（财务系统命脉）
- 数据库**每天自动备份**（pg_dump），保留近 N 天。
- 备份**至少再拷一份到另一介质**（移动硬盘/网盘）。
- 定期做一次"恢复演练"，确认备份真能还原。

## 6. 日常更新（`update.bat`）
1. **先备份**：跑 `backup.bat`（或手动复制 `db.sqlite3`），尤其是带**数据迁移**的更新。
2. 跑 `update.bat`：拉取 Gitee `master` → `uv run python manage.py migrate` → `collectstatic` → 重启 `InventoryERP` 服务。
3. **带数据迁移的更新务必先备份**：迁移会改库里既有数据，且不一定可逆。升级后抽查一两个关键页面/余额确认无误。

### 6.1 升级提示（按时间倒序，影响既有数据的重点记这里）
- **2026-06-17　应收票据「收票抵应收账款」口径修正（迁移 `0019`）**
  - **变更**：收到客户票据抵应收账款 = 借应收票据/贷应收账款——**冲应收账款只减应收账款、不再消耗票据**；票留持有（在手、未用=票面），之后可背书/托收。旧逻辑把"核销应收"也当成把票用掉，使持有的票从账上消失（资产少计）。
  - **迁移 `0019_recompute_note_receivable_settled` 会自动**把历史每张应收票据的"已用"重算为**仅背书合计**、并重置状态（未用>0→在手，用尽→已背书）。
  - **升级后数据会变（属正确修正，非异常）**：三家**应收票据余额上升**（把本就持有、之前被错误注销的票补回账）；**应收账款余额不变**（之前的冲减是对的）；之前因"核销应收"被标「已结算」的票会变回「在手」、重新可背书/托收。
  - **务必先 `backup.bat`** 再 `update.bat`；升级后核对 C3 等账套的应收票据余额表与几张相关销售发票的已核销额。
