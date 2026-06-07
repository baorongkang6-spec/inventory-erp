# 开发随手记（DEV_NOTES）

> 可复用经验库：重要架构决策 / 可复用模式 / 踩坑及解法。
> 目的：将来抽取「ERP 起步模板」时有现成素材。要点即可，做完一块顺手追加。

---

## 一、架构 & 技术栈决策

- **uv 管一切**：Python 版本 + 依赖 + venv，统一 `uv run python manage.py ...`。Django **5.2 LTS** 配 **Python 3.13**（5.2 LTS 不支持 3.14，求稳不用 6.0）。
- **应用命名空间**：业务代码放 `apps/`，每个 app `name = "apps.xxx"`，避免与三方包重名。
- **配置全 env 化**：`SECRET_KEY/DEBUG/ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS/DB_ENGINE` 走环境变量；`DB_ENGINE` 切 SQLite(开发)/PostgreSQL(生产)；全局 `ATOMIC_REQUESTS=True`（金额/库存操作天然在事务内）。
- **前端本地化**：Bootstrap5 vendored 到 `static/vendor/`，不依赖外网（部署是本地/可能离线主机）。
- **多公司账套地基**：`core.CompanyScopedModel` 抽象基类（`company` FK + 创建/更新时间 + created_by）；数据范围逻辑集中在 `core/scope.py`；`core/context_processors.py` 把「可见公司 / 当前账套 / 菜单」注入所有模板。

## 二、可复用模式

- **通用公司维度 CRUD**：`core/crud.py` 的 `ScopedListView/Create/Update/Delete` + `templates/crud/` 通用模板；子类只声明 model/form/columns/url 名。masterdata、finance 银行账户都复用。
- **精度与舍入集中**：`core/money.py` —— 数量 3 位、单价/金额 2 位、`ROUND_HALF_UP`（中国财务习惯，区别于 Python 默认银行家舍入）；`round_money/round_qty`、`DEFAULT_TAX_RATE`。
- **单据编号**：`core/docnum.py` `next_doc_no(model, company, 前缀, 日期)` → `前缀-公司编号-yyyymmdd-序号`（零填充，按 doc_no 倒序取当日最大）。前缀表：入库 RK / 出库 CK / 采购发票 CGF / 销售发票 XSF / 付款 FK / 收款 SK / 应收票据 YSP / 应付票据 YFP。
- **表单基类**：`core/forms.py` `BootstrapForm`/`CompanyScopedModelForm`（见踩坑「公司内唯一」）。
- **RBAC**：`ModelPermRequiredMixin` 按「动作+模型」推导 Django 权限点；权限→角色映射集中在 `seed_init.ROLE_PERMS`（用 `app_label.codename` 跨应用）；模板用 `perms.*` 控按钮/菜单显隐；特殊动作（如作废）用 `Meta.permissions` 自定义权限（`void_*`）。
- **过账走服务层**：业务写操作集中在各 app 的 `services.py`，函数 `@transaction.atomic`；模型只存数据。**单据保存即过账**（无审核流，符合本项目）。
- **「单据即往来」**：发票本身就是应付/应收单据 —— `amount_taxed`(原始额) + `settled_amount`(已核销) + `outstanding`(派生)；核销/冲销用独立 allocation 表，校验不超额、**任一行违规整体回滚**。
- **结存 + 流水快照**：`StockBalance`(权威当前结存) + `StockMove`(不可变流水，含过账后 数量/金额/均价 快照) → 直接喂「数量金额式台账」和对账。同样思路用于银行日记账。
- **移动加权平均**：`(数量, 金额)` 为权威值，均价 = `round(金额/数量, 2)` 派生；出库到清零时成本 = 剩余全部金额，使金额精确归零（消除舍入残值）。不允许负库存。
- **跨账套联动**：客户/供应商挂 `related_company` 指向系统内公司；触发时**同一事务内**在对方账套生成镜像单据 + 双向互链；商品按编码在对方自动配齐；作废用 `inventory.reverse_move` 精确反向冲减、级联作废镜像、负库存则拒绝。
- **Excel 导入导出**：`openpyxl`；**导出的前 N 列与导入格式对齐**，支持「导出→编辑→再导入」往返；导入按业务键去重（如银行流水按 账户+日期+方向+金额+摘要+对方；票据按票据号）。
- **期初数据**：用 `is_opening` 标记 / `source_type="Opening"` 流水，**复用现有单据与流水管道**（期初应付=is_opening 采购发票、期初库存=Opening 入库流水、期初银行=BankAccount.opening_balance、期初票据=is_opening 票据），不另起新表；总览/对账据此区分「期初 vs 本期」。期初导入按编码匹配主数据、按业务键去重防重复。
- **总览四列勾稽**：恒等式 `期初 + 本期收入 − 本期发出 = 期末`。口径：期初=期初标记数据，本期=全部非期初活动，期末=当前余额；系统只有一个区间时无需按业务日期切分。聚合用 `Sum`，金额维度（库存数量异构不跨商品相加）。
- **对账模式**：系统侧逐行算余额 + 录入外部值 → 差异，持久化 `ReconciliationRun + Line` 快照；差异≠0 标红。
- **费用计入成本分摊**：入库其他费用中「计入成本」的合计，按各行基础金额比例分摊、**余数归最后一行**保证合计精确；通过 `post_inbound(amount=...)` 覆盖入库金额（单价反算）抬高移动加权。不计入的作期间费用记录（`ExpenseEntry`），出库费用一律期间费用。
- **借调往来**：独立 `BorrowTransaction` 台账（借入 IN+ / 归还 OUT−，按对手单位汇总），借调不涉税、不挂应付（区别于发票产生的应付）；借调入库挂往来、归还出库冲减。
- **借调联动复用销售镜像**：`sales_type`（销售/借出/归还）决定对方账套镜像入库是「外购」还是「借调」；借调镜像两侧 `BorrowTransaction` 反向对冲；作废级联同时撤销两侧往来。

## 三、踩坑 & 解法

- **`<input type=date>` 取不到值**：zh-hans 本地化日期格式非 ISO，浏览器 date 控件渲染为空 → 提交丢值/必填报错。解：日期 widget 统一 `format="%Y-%m-%d"`（已在 `core/forms.style_fields` 和各 ModelForm widget 处理）。
- **公司内唯一约束失效**：`company` 不在表单字段里时，Django 会把它从唯一性校验中排除 → `(company, code)` 约束跳过 → 最终 IntegrityError 500。解：`CompanyScopedModelForm._get_validation_exclusions()` 里 `discard("company")`，并在视图校验前给 `instance.company` 赋值（`CompanyScopedFormMixin`）。
- **DB_ENGINE 写法不一致**：DEPLOY 写 `postgres`、settings 只认 `postgresql` → 生产静默回退 SQLite（危险）。解：大小写不敏感接受 `{postgres, postgresql}`。
- **多 form 页面的提交误触**：登录后布局里有多个 form（退出/账套切换/业务表单），用 `button[type=submit]` 泛选会点到导航里的退出按钮。解：用目标表单内唯一字段定位 `xxx.closest('form').requestSubmit()`。
- **Bash 工作目录偶发重置**：cwd 有时回到主工作区，导致 `manage.py` 找不到。解：`git -C <repo>`、`cd <repo> &&`、或 `uv run --directory <repo>`。
- **openpyxl 读行**：用 `ws.iter_rows(values_only=True)`（不是 `values_list`）。
- **作废 ≠ 删除**：移动加权下「真正撤销」很难；采用记 `status=void` + 反向补偿流水（reverse_move）而非物理删除/重算历史；反冲若致负库存则拒绝（提示先冲后续业务）。
- **流水缺业务日期**：`StockMove` 只有 `created_at`（时间戳）无 doc_date，做**按业务月份**的报表会不准。本期用「期初标记区分」规避；将来要做多期间报表/结账，需给流水加 `date` 字段（从单据 doc_date 带入）。
- **get_or_create 不更新已存在行**：seed 里改 `defaults`（如给已建的银行账户加 opening_balance）对**已存在**记录无效，只影响新建。改既有数据要显式 update 或重建库。

## 四、安全加固（上线前）

- **生产开关与测试解耦**：用显式 `DJANGO_PRODUCTION` env 控制 HTTPS/安全 Cookie/HSTS，**不要用 `not DEBUG`**——Django 跑测试时默认 `DEBUG=False`，`SECURE_SSL_REDIRECT` 会把测试请求 301 到 https 致全部失败。
- **生产 fail-fast**：`PRODUCTION` 下若 SECRET_KEY 仍是开发默认 / `DEBUG=1` / 缺 `CSRF_TRUSTED_ORIGINS` → 启动即 `ImproperlyConfigured`，避免裸奔上线。
- **反向代理 HTTPS**：花生壳等终止 HTTPS 后转发 http，须设 `SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO","https")`，否则 `SECURE_SSL_REDIRECT` 死循环。
- **登录防爆破（自实现）**：用户名+IP 用 Django 缓存计数，达限锁定；`user_login_failed/user_logged_in` 信号计数+清零+写 `AuditLog`。比 django-axes 轻、无额外 auth 后端坑（axes 的 AxesStandaloneBackend 会干扰 `force_login` 测试）。⚠️ LocMemCache 是**单进程**的，多进程/多机部署需换共享缓存（Redis）。代理后取真实 IP 用 `X-Forwarded-For` 首段。
- **静态文件**：纯 WSGI（Waitress）不会自动服务 static，用 **WhiteNoise**（中间件紧跟 SecurityMiddleware）+ 生产 `CompressedManifestStaticFilesStorage`（须先 `collectstatic`）；开发不启用 manifest，否则未 collect 时 `{% static %}` 报错。
- **.env 要真正加载**：写了 `.env.example` 不等于生效——`settings` 里 `load_dotenv(BASE_DIR/'.env')` 才会读。
- **校验**：`manage.py check --deploy`（带生产 env）应零告警；`SECRET_KEY` 须 50+ 位随机（否则 W009）。

## 五、M7 增强（多期间报表 / 下钻 / 筛选）

- **`StockMove` 加业务日期**：补 `date`（默认 `localdate`，从单据 `doc_date` 带入），并迁移回填历史（`date=created_at.date()`）；自此总览/报表全按业务日期口径（银行 `date`、库存 `move.date`、发票 `doc_date`、票据 `draw_date`；核销无落库日期，用 `created_at.date()` 近似——已知限制）。
- **报表下钻不改账套**：`scope.resolve_company(request)` 读 `?company=`（须在可见集合内）否则退回当前账套，让总览行可直接点进「某公司」明细，而不必先切账套。
- **账户余额表（明细账户级）**：`opening/reports.py:account_balance_table()`——银行按账户、库存按品种、应付按供应商、应收按客户的 期初/本期收入/本期发出/期末。AP/AP 减项需归属到往来对象：付款核销走 `allocation.invoice.supplier/customer`，票据冲销按 `invoice_id` 反查对手；数据量小直接 Python 归并（非 SQL group by）。四列恒等 `期初+收入−发出=期末` 用断言守。
- **通用列表筛选（`FilteredListMixin`）**：声明 `search_fields`（支持跨表 `supplier__name`，`Q` 或连）+ `date_filter_field`（`?from/?to` 区间）即得筛选条，模板 `{% include "_filter_bar.html" %}` 统一渲染（按 `has_q/has_date` 自适应）。`ScopedListView` 已内置，自定义 ListView 加 mixin 即可。重置即 `request.path`。
- **通用 Excel 导出**：`core/exports.py:xlsx_response(filename, headers, rows)` 统一出包；`FilteredListMixin` 加 `export_columns=[(表头, accessor)]`（accessor 支持 `a__b` 跨级、callable、`get_x_display` 自动调用）即得「导出 Excel」按钮，**沿用当前筛选**（导出链接 = 当前 querystring + `export=xlsx`）。报表（总览/账户余额/库存/各余额表）在视图里判 `?export=xlsx` 直接调 `xlsx_response`。`_norm()` 把 Decimal→float、日期→`%Y-%m-%d`、模型实例→str，避免 openpyxl 写入报错。
- **银行流水增量导入按流水号去重**：`BankJournal` 加 `txn_no`（交易流水号）；导入模板/导出均含该列。判重优先级：**有流水号→「账户+流水号」**（最稳，可重复上传整月网银流水、改摘要不影响判重，已存在的不覆盖），无流水号→退回「账户+日期+方向+金额+摘要+对方」。批次内部也用 `seen_txn/seen_content` 去重，避免同文件重复行。导出 HEADERS 第 6 列放流水号、第 7 列放只读余额，导入只读前 6 列（`IMPORT_COLS`）保证「导出→编辑→再导入」往返。

## 六、M8 银行收支与对账（混合流）

- **流程定调（混合流）**：银行存款日记账=完整资金账本。往来收付仍在源头登记（收款/付款，挂 AR/AP 可核销，关联镜像依赖单据带明确对象）；非往来（费用/税/工资/内部划转/其他）走「其他收支登记」或导入；Excel 导入定位为**对账工具**而非猜发票。
- **日记账业务类型**：`BankJournal.entry_type`（settlement/expense/tax/payroll/transfer/other），付款收款自动标 settlement，其余默认 other；迁移按 `source_type in (Payment,Receipt)` 回填。日记账报表加类型列+筛选+本期合计；**类型筛选时逐笔余额仍按全部流水累计**（保持账户真实余额），只隐去非该类型行。
- **其他收支登记**：`create_other_cashflow` 直接成日记账（source_type=Other），拒绝 settlement 类型（那走收付款）；`delete_other_cashflow` 仅允许删 Other（系统生成/往来的不可删，避免破坏核销）。
- **银行对账**：`reconcile_bank_journal` 匹配优先级「账户+流水号 → 账户+日期+金额+方向」，同一系统行只配一次；匹配的置 `reconciled=True`+记 `BankReconcileBatch`。对账期间 = 导入流水日期跨度；**"仅系统有"只在该期间内判定**（期间外的系统流水不算漏报）。
- **行内一键补录的坑**：`<form>` 不能直接嵌在 `<tr>` 里（浏览器会把它拆到表格外，hidden 输入失效）。解法：表单元素放表格外、行内 `<select>/<button>` 用 HTML5 `form="<id>"` 关联。

## 七、M9 总览两级下钻（各类目→分项余额表→明细账）

- **统一模式**：总览每类目点「类目-公司」→ 第一层「分项余额表」(期初/本期收入/本期发出/期末，带日期窗口)，再点某项→ 第二层明细账(滚动余额，期初/期末，当期无发生也显示期末)。全程 `?company=` 下钻不改账套。
- **复用聚合**：`reports.py` 抽出 `bank_accounts_balance`/`stock_products_balance`/`payable_partners_balance`/`receivable_partners_balance`/`receivable_notes_balance` 第一层；`partner_ledger(kind)`/`note_ledger`/库存台账 第二层。`account_balance_table` 各段改为复用这些 helper，避免算法漂移。`_partner_rows` 重构为 `_partner_balance`(保留 partner 对象) 的包装。
- **明细账滚动余额口径**：增=发票/出票，减=核销/票据抵付/使用；期初=区间前净额，逐笔累计；票据明细的「余额」是未用额。事件按 (日期, 增在前) 排序。
- **库存明细账**：逐行结存直接用 StockMove 存的 `balance_quantity/price/amount`(过账时快照、含均价)；期初=区间前 signed 累计、期末=末笔结存，二者与快照一致。
- **plain `manage.py shell` 验证陷阱**：非 TestCase 的 `Client()` 不挂 `store_rendered_templates` 信号 → `response.context` 为 None。冒烟脚本要么断言 `response.content` 文本，要么写进 TestCase 用 `resp.context`。

## 八、M10 单据可点击 + 术语统一

- **来源单据跳转**：`core/docrefs.py` 集中维护 `source_type → 详情路由名` 映射（PurchaseInbound/SalesOutbound/Payment/Receipt/Purchase·SalesInvoice），`doc_url(type,id)`/`invoice_url(kind,id)` 无对应详情(期初/作废反冲/其他收支)返回空串。库存明细账、银行日记账、往来明细账、票据使用明细的单据号都据此渲染成链接(无则纯文本)。
- **明细账事件带 ref_url**：`partner_ledger`/`note_ledger` 在生成事件时就算好 `ref_url`(发票→发票详情、核销→付/收款详情、票据抵付→被冲发票)，模板只判空显示链接，避免模板里塞映射逻辑。
- **术语两类统一**：单据**自身编号**列头统一「单据编号」(各列表+导出表头)；**引用来源单据**的列统一「来源单据」(台账「单据」、日记账「来源」改名并可点)。语义不混。
- 改列头后注意同步**导出表头**(export_columns 第一元素)和断言旧表头的测试。

## 九、M11 查询中心（跨公司组合查询）

- **注册式事项**：`opening/query.py` 的 `SUBJECTS` + `_RUNNERS`，每个查询事项一个 `_q_*(companies, dfrom, dto, params)` 返回 `{columns, rows, totals}`(统一二维结构,模板/导出通用)。新增事项只加一段函数 + 注册。
- **跨公司**：公司多选(checkbox),`company__in=chosen`;默认勾选全部可见公司,可取消。范围始终先按 `get_visible_companies` 过滤。
- **跨公司商品/往来用关键字而非下拉**:C1/C2/C3 各有各的 Product/Customer 行,跨公司没有统一主键,所以筛选用 code/name 关键字(icontains),避免下拉只能选一家。
- **合计列**:`_totals(columns, idxs, rows)` 只对金额列求和(数量异构不跨商品相加);首列「合计」。
- 事项相关筛选(方向/业务类型/状态)由 `meta.extra` 声明,模板按需渲染对应控件。导出复用 `xlsx_response`,把合计行附在末尾。

## 十、M14 单据修改（采购入库/销售出库）

- **机制=冲正重过账**：`update_and_repost_*` 在事务内"反向冲减原过账→按新明细在同一张单上重过账"(保留单号)，避免改移动加权历史的复杂重算；创建/修改共用 `_apply_*_lines` 抽出的过账逻辑。
- **可改性守卫**(`*_edit_block_reason`)：①本人或管理员(管理员=超管或有 void 权限)；②本月内(跨月不可改，会计期间控制)；③未被下游引用(已开发票/关联镜像 → 拒绝，提示作废重录)。镜像生成的入库单、已生成镜像的出库单都不可直接改。
- **审计**：修改记 `AuditLog.UPDATE`。入口：单据详情页「修改」按钮；列表(带筛选)→单号→详情→修改即"查询界面可改"。

## 十一、发票×单据数量勾稽（已出库未开票 / 已入库未收票明细表）

- **背景**：发票要能按"出库/入库数量 − 已开票/已收票数量"算未结差额，必须让发票行存数量并真正关联源单据行。早期销售发票行只复制金额、不写 `source_outbound_line`，导致全部当"独立"无成本——这是大坑，采购侧同理。
- **数据模型**：`SalesInvoiceLine` / `PurchaseInvoiceLine` 各加 `quantity`(decimal 18,3, 默认 0) + 已有的 `source_*_line` 外键(SET_NULL，独立录入时空)。两侧完全对称。
- **联动链路**(改一处必改全链)：①`_*_prefill` 带出 `quantity` + `source_*_line=ln.pk`；②表单加 `quantity` 字段 + 隐藏 `source_*_line`(IntegerField/HiddenInput)；③模板渲染数量列 + `{{ form.source_*_line }}`；④视图 `_resolve_*_line(company, id)` 把 id 解析成限本账套的行对象，create/update 写入；⑤service `create_*_invoice` 存 `quantity=ln.get("quantity") or ZERO_QTY`。
- **报表**：`shipped_uninvoiced` / `received_uninvoiced`(opening/reports.py) 对称——遍历出库/入库行，已开票/收票数量=关联本行的发票行 `quantity` 之和(排除作废发票)，差额≠0 才列出；金额按源单行单价×未结数量换算不含税/含税(`round_money`)。`received_uninvoiced` 作为库存**暂估**依据。
- **多公司**：视图收 `request.GET.getlist("company")`，不选=全部可见公司；导出仅单公司时才把 company 传给 `xlsx_response`(影响编制单位)。权限：销售侧 `view_salesinvoice`，采购侧 `view_purchaseinvoice`。

## 十二、移动加权下入库单不可乱改（精确反冲的边界）

- **现象**：改一张入库单报"库存不足：现有100，需出库100"，数量明明相等却失败——误导。
- **真因**：入库修改/作废走"精确反冲原过账"。`reverse_move` 反冲入库要从结存扣回**原数量+原金额**，判断 `move.quantity > bal.quantity or move.amount > bal.amount`。移动加权下，本批货若已被后续出库按**混合均价**卖出，其成本被摊出，结存金额(3820.58)会小于本批原值(4424.78)——**金额条件**触发失败，但旧报错只印数量，故看着是 100 vs 100。
- **修法**：①`reverse_move` 金额/数量任一不足时，`InsufficientStockError` 用 `message=` 传**含金额的清晰提示**（保留子类型，兼容既有"镜像被消耗禁止作废"测试）；②`inbound_edit_block_reason` 加 `_inbound_reverse_block_reason(doc)` 预检（按商品聚合本单各 stock_move 的 qty/amount，与 `StockBalance` 比对），点「修改」即拦截并提示"请先作废引用本批货的出库单"，不必填完整张表才报错。
- **结论**：这是设计边界不是 bug——一批货一旦被后续出库消耗就无法干净倒带；要改先作废下游出库单（顺序反向）。出库侧反冲是"加回库存"永不下溢，无此问题。

## 十三、银行存款日记账多公司 + 全部账户

- **需求**：日记账支持多公司联合查询；银行账户不选=全部账户。
- **多账户余额语义**：跨账户没有统一滚动余额。`_journal_rows_multi(accounts,…)` 对每个账户各自调 `_journal_rows` 内部滚动，再合并按「公司/账户/日期」排序；每行「余额」是**该账户**自身的滚动余额，期初/期末为所选账户**合计**。单选账户时退化为经典存折式（multi=False）。
- **视图**：公司多选(getlist)不选=全部可见公司；账户下拉加「全部账户」(value="")。模板 `multi` 标志控制是否显示 公司/账户 两列及合并提示。
- **导出**：单账户沿用 `export_bank_journal`(存折式)；全部账户走通用 `xlsx_response`，含 公司/银行账户 列与期初/合计/期末行。导出链接把 `chosen_ids` 一并带上保持口径一致。

## 十四、余额类报表统一多公司

- **范围**：账户余额表、应付/应收账款余额表、票据余额表、借调往来余额表全部支持多公司联合查询（公司不选=全部可见公司），各加「公司」列。
- **统一作用域**：`finance/views.py` 加 `_company_scope(request)`→(visible, chosen)，所有余额报表共用；账户余额表在 `opening/views.py` 内联同款逻辑（其报表函数 `account_balance_table` 本就收公司列表）。
- **通用过滤片段**：`templates/_company_filter.html`（需 `visible_companies`+`chosen_ids`），各报表 `{% include %}`；导出链接统一 `?{% for cid in chosen_ids %}company={{ cid }}&{% endfor %}export=xlsx` 以保留多选。
- **分组**：应付/应收按 (公司, 往来对象) 分组（`_outstanding_balance_report` 通用化 payable/receivable）；借调按 (公司, 对手单位) 聚合；票据按公司排序平铺。导出仅单公司时才把 company 传 `xlsx_response`（影响编制单位）。
- **未动**：M9 从总览下钻的 `payable_partners_report`/`receivable_partners_report`/`receivable_notes_report` 仍是单公司下钻（带 company_id + 返回链接），与跨公司总览配套，不混入多选。

## 十五、菜单版应付/应收余额表改为余额式 + 明细账下钻

- **变更**：「报表」菜单的应付/应收账款余额表由「未核销发票清单」改为「按公司·往来对象的 期初/本期增加/本期减少/期末余额」，加日期区间，点供应商/客户进该公司该对象的往来明细账。与 M9 总览下钻同形态，但**多公司联合**。
- **复用**：直接调 `payable_partners_balance/receivable_partners_balance(company,dfrom,dto)` 逐公司汇总再合并（这俩本就返回 opening/income/outgo/ending + partner 对象）；明细账复用既有 `payable_partner_ledger/receivable_partner_ledger`（`resolve_company` 已支持 `?company=`，故跨公司下钻无需改后端）。
- **共用明细账两入口**：ledger 加 `src=menu` 区分来源——决定「返回余额表」回菜单报表还是 M9 总览下钻报表；`src` 在 ledger 自身的查询表单/导出链接里透传。
- **模板**：新增 `partner_balance_multi.html`（公司列 + 多选 + 合计）；旧 `balance_report.html` 及 `_outstanding_balance_report` 的发票清单实现删除。
