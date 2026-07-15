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

- **`StockMove` 加业务日期**：补 `date`（默认 `localdate`，从单据 `doc_date` 带入），并迁移回填历史（`date=created_at.date()`）；自此总览/报表全按业务日期口径（银行 `date`、库存 `move.date`、发票 `doc_date`、票据 `draw_date`；核销业务日期见迁移 `0021`——已补 `date` 字段，取付款/收款 doc_date 或票据 draw_date，不再用 `created_at` 近似）。
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

## 十六、允许负库存 / 负应收应付（控制放开）

- **负库存**：`post_outbound` 去掉"库存不足"拦截，结存数量/金额可为负；库存=0 仍出库时用最近均价(通常 0)作成本基准、避免除零，待入库自然修正。`reverse_move` 反冲入库不再校验下溢。删掉采购入库修改的"已被消耗不可改"预检(`_inbound_reverse_block_reason`)。`InsufficientStockError` 类保留（已无处抛出，sales/views 的 except 成无害死代码）。
- **负应收/应付**：①发票行可填负数(红字/退货)——finance 两个发票行表单去掉 `amount_untaxed`/`quantity` 的 `min_value=0`，clean 改为"金额不能为 0、可为负"。②核销可超过发票未核销额——`allocate_payment`/`allocate_receipt`/票据冲销去掉"单张发票不得超额"校验，使发票 outstanding 可为负(预收/预付)；**保留**"核销合计 ≤ 付款/收款额、≤ 票据未用额"这两条完整性控制。
- **注意**：只放开了 finance **发票**行的负数；采购入库/销售出库(实物单)的金额仍 `min_value=0`（负库存靠数量出超实现，不是靠负金额）。
- 受影响测试已改为"允许"语义（inventory/sales/finance/tests_interco）。

## 十五、菜单版应付/应收余额表改为余额式 + 明细账下钻

- **变更**：「报表」菜单的应付/应收账款余额表由「未核销发票清单」改为「按公司·往来对象的 期初/本期增加/本期减少/期末余额」，加日期区间，点供应商/客户进该公司该对象的往来明细账。与 M9 总览下钻同形态，但**多公司联合**。
- **复用**：直接调 `payable_partners_balance/receivable_partners_balance(company,dfrom,dto)` 逐公司汇总再合并（这俩本就返回 opening/income/outgo/ending + partner 对象）；明细账复用既有 `payable_partner_ledger/receivable_partner_ledger`（`resolve_company` 已支持 `?company=`，故跨公司下钻无需改后端）。
- **共用明细账两入口**：ledger 加 `src=menu` 区分来源——决定「返回余额表」回菜单报表还是 M9 总览下钻报表；`src` 在 ledger 自身的查询表单/导出链接里透传。
- **模板**：新增 `partner_balance_multi.html`（公司列 + 多选 + 合计）；旧 `balance_report.html` 及 `_outstanding_balance_report` 的发票清单实现删除。

## 十七、全站数字千分位 + 两位小数

- **网页**：zh-hans 内置把 THOUSAND_SEPARATOR 设空、分组 4 → 金额无千分位。解决：自定义格式模块 `apps/formats/zh_Hans/formats.py`(THOUSAND_SEPARATOR="," NUMBER_GROUPING=3) + `FORMAT_MODULE_PATH=["apps.formats"]` + `USE_THOUSAND_SEPARATOR=True`。只读输出自动分组；表单 number 输入不分组(已验证不影响录入)。
- **长尾零根因=SQLite**：对 DecimalField 做 SUM 返回浮点→Decimal 带长尾零。reports 的 `_s`/`bank_open0` 用 round_money、收开票数量 round_qty 量化。
- **Excel**：xlsx_response 数字单元格 + 银行日记账导出金额列(D/E/G) 设 `number_format="#,##0.00"`。
- 备注：数量仍按 3 位小数显示(保精度)；金额 2 位。

---

## 进度快照（2026-06-12）

> 交接存档：新 session 读 `CLAUDE.md` + `SPEC.md` + 本节即可无缝接续。

### 一、项目当前状态
- **已上线生产，进入"按需迭代"阶段**，早已远超 SPEC §11 的 M0–M6 里程碑（M0–M10 均完成，见 TaskList）。
- 生产部署：公司 Windows Server 2022（内网 `192.168.10.245`），**SQLite** + Waitress + NSSM 服务（`InventoryERP`，开机自启），花生壳 HTTPS `https://10vtcu2238888.vicp.fun`，Gitee 私有仓库（SSH 免密）远程更新（`update.bat`）。
- **全量测试 190 个全绿**（`uv run python manage.py test`）。`manage.py check` 无警告。`git` 工作区干净、全部已推送 Gitee `master`。
- **2026-07-03　生产已更新到 `56e7db4` 并核对通过**：本次带数据迁移 `0021`（核销业务日期回填）。生产机原停在 `ba3753a`（落后 6 个提交），本次一并补齐；先手动备份（服务停→拷 db 到 `C:\erp_backup`）再迁移。升级后核对 C3 客户往来明细账：票据抵付各行日期显示为票据出票日 `2026-06-22`（非操作当日），口径修复生效。
  - 运维踩坑：生产机若落后于 `dc01786`（"update 前自动备份 + sqlite backup API"），其在盘 `update.bat`/`backup.bat` 仍是旧版；直接跑旧 `update.bat` 会在 `git pull` 中途替换自身导致 cmd 边读边跑错乱。**落后多个提交时改手动分步**：`.\nssm stop` → 手动备份 → `git pull` → `uv sync` → `migrate` → `collectstatic` → `.\nssm start`。PowerShell 里 `nssm` 需写 `.\nssm`。

### 二、本轮（近几个 session）做完的增量（均已测试+提交+部署）
0. **应收票据「兑付/贴现」可改（2026-07-14）**：录错兑付/贴现日期无法更正（旧只能撤销重录）。加 `update_note_disposal` + `note_disposal_edit_block_reason`——只改**日期/收款银行账户/备注**（金额、贴现息、票据消耗不变），**同步对应银行日记账的日期与账户、贴现息费用记录的日期**；票据作废或银行日记账已对账则拦截（避免破坏对账批次）。入口：应收票据使用明细处置行「撤销」旁加「修改」，`note_disposal_edit.html` 表单；权限沿用 `add_notesettlement`，`next` 安全回跳。改金额仍走「撤销重录」。
1. **应收票据收付集成**：收款方式/付款方式下拉加「应收票据」——收款=银行账户+收到应收票据；付款=银行账户+应收票据背书抵应付。复用既有 `create_note_receivable / settle_receivable_against_sales / endorse_receivable_against_purchase`，不重写账务内核。
2. **发票尾差手工微调**：采购/销售发票行的「税额」「含税金额」可手填（`_resolve_tax` 优先用录入值），尾差不再被自动算覆盖。
3. **采购发票「修改」**：`update_purchase_invoice` + `purchase_invoice_edit`（对齐销售发票，保留单号、重算应付）。
4. **各列表「修改」快捷入口**：采购入库/销售出库/采购发票/销售发票列表操作列加「修改」。
5. **收款/付款「修改」「删除」**：仅「当月+未核销+未对账」；修改同步对应银行日记账，删除连带删日记账。`_cash_doc_block_reason` 统一判定。
6. **其他收支「修改」**：`update_other_cashflow`（仅手工 source_type=Other + 当月 + 未对账）；银行日记账报表行加「修改/删除」。
7. **应收票据可分次混合使用（修了真 bug）**：`_apply_note` 原先一背书就锁「已背书」、剩余票面用不了；改为**仅票面全用完(unused==0)才定终态**，未用完保持「在手」，可"部分冲应收 + 部分背书"混用。终态：含任一背书→已背书，否则已结算。
8. **采购/销售发票「删除」**（彻底移除，区别于作废）：`delete_purchase_invoice / delete_sales_invoice`，未核销+非期初才可删；列表+详情都有按钮（含已作废未核销的清理）。
9. **采购入库「受限硬删除」**：`delete_purchase_inbound` + `inbound_delete_block_reason`。因移动加权成本链式，**仅当该入库后相关商品再无任何出入库变动**（即"刚录错马上撤")、且非镜像/未开票/当月/本人或管理员时才允许；用 `reverse_move` 精确反冲再删两条流水不留痕。其余情况用「作废」。
10. **修改收付时可切换为票据**：付款「修改」改用完整 `PaymentForm`、收款「修改」改用完整 `ReceiptForm`；若把方式改成应收票据(背书)/应收票据，保存时**原子地删旧银行单+日记账、改记为票据**（误记更正）；校验失败整体回滚。删掉了 `PaymentEditForm/ReceiptEditForm` 与 `cash_doc_edit.html`。
11. **发票详情「核销明细」**：列出已核销来自哪些付款/票据（`_invoice_settlements` 合并 PaymentAllocation/ReceiptAllocation + NoteSettlement，标明"应收票据背书抵付"等）。解决用户疑惑"已核销但付款里没有"。
24. **采购入库列表加「作废」快捷按钮（2026-06-26）**：对称销售出库；门控加 `not doc.source_outbound_id`（镜像生成的入库单不可直接作废，要作废源销售出库）。复用 `inbound_void`(已 `@require_POST`)。
23. **销售出库列表加「作废」快捷按钮（2026-06-26）**：原作废仅在详情页；列表操作列加「作废」(POST 表单 + confirm，门控 `status != void` + `perms.sales.void_salesoutbound`，与详情页一致)。复用 `outbound_void`(已 `@require_POST`)，有镜像则联动作废。
22. **关联销售镜像改按不含税售额入库（2026-06-26）**：`_mirror_to_related_company` 原一律按 A 结转成本平移（M4 时出库无售价的限制）。M7 后出库有三价，故**销售镜像改为：B 入库成本=源出库行不含税售额**（含税/税额一并镜像，便于 B 收采购发票勾稽），无售价(amount_untaxed=0)则回退成本；**借调镜像仍按成本平移**(不涉税)。传 `amount_untaxed/tax_rate/tax_amount/amount_taxed` 给 `create_and_post_inbound`(入库成本=不含税)。SPEC §5.1 已改。修历史数据：作废源出库重录(镜像单不可直接改)。
21. **应收票据 到期兑付 / 贴现（M16，2026-06-17）**：票据第三/四种退出方式——「票据→银行存款」(区别于背书=票→抵应付、核销应收=票进来)。新模型 `NoteDisposal`(kind=collect/discount, 真 FK 到 NoteReceivable + bank_account/bank_journal/expense)；新 `BankJournal.EntryType.NOTE_CASH`(票据兑现)。`collect_note_receivable`(票面进银行)/`discount_note_receivable`(实收净额进银行+贴现息=票面−净额记 `ExpenseRecord` 财务费用)，都 `_consume_note_to_cash` 消耗票面(settled_amount += 票面，沿用"出账侧才算已用"口径)、生成银行日记账。`reverse_note_disposal` 撤销(恢复票+删日记账+删费用)。报表三处把 disposal 计入票据"出去"减项(company_overview/receivable_notes_balance/note_ledger)。`note_has_usage` 也查 disposal。入口：应收票据列表「到期兑付/贴现」按钮(can_cash=未用>0且非作废)；`note_cash.html` 表单；撤销在票据使用明细。贴现输实收净额(用户确认)。拒付/退票暂不做。
20. **采购入库列表加不含税合计列 + 合计（2026-06-17）**：对称销售出库——`InboundListView` 加「不含税合计」(`total_untaxed`，在入库成本与含税合计之间)；`totals.amount/untaxed/taxed` tfoot 合计。
19. **销售出库列表加不含税售额列 + 合计（2026-06-17）**：`OutboundListView` 表头/导出加「不含税售额」(`total_untaxed`，在含税左侧)；`get_context_data` 算 `totals.untaxed/taxed/cost`（金额列求和，总数量异构不加）；tfoot 合计的结转成本列同样受 `perms.inventory.view_amount` 门控。
18. **应收票据列表「票面」拆期初/本期（2026-06-17）**：按 `is_opening` 把票面拆「期初金额 / 本期收入」两列（含 tfoot 合计 `totals.opening/period` 与导出 `export_columns` 用 callable `lambda n: n.amount if n.is_opening else ""`）。与总览/票据余额表的期初口径一致。
17. **列表合计行（2026-06-17）**：应收票据列表（票面/已用/未用）、收/付统一一览（金额/已核销/未核销）加底部 tfoot 合计。note 列表在 `NoteReceivableListView.get_context_data` 算 `totals`；收付列表用 `_cash_totals(rows)`（对**过滤后**的 rows 求和，故随筛选/日期变化）。模板 `{% if rows %}`/`{% if notes %}` 才显示。
16. **应收票据口径修正：收票抵应收不消耗票面（2026-06-17）**：原 `_apply_note` 把「核销应收账款」也计入 `settled_amount`（消耗票面），等于把客户给的、你仍持有的票从账上抹掉（资产少计）。正确口径=借应收票据/贷应收账款：**冲应收只减应收账款发票、不消耗票；票留持有(未用=票面)，可背书/托收**。改法：`_apply_note` 按 `consumes = is_endorsement or note_kind==PAYABLE` 分流——消耗侧(背书/应付票抵应付)减未用额、到 0 定终态；非消耗侧(核销应收)只减发票、上限=票面−`_note_applied_ar`(已抵应收合计)，不动票。`reverse_note_settlement` 同样按 consumes 分流(撤核销应收只退发票、撤背书才退票)。报表只让"票出去"减票据持有：`company_overview`/`receivable_notes_balance`/`note_ledger` 的票据减项加 `is_endorsement=True`（应收账款减项仍用 is_endorsement=False，不变）。`note_has_usage()`(有任何 NoteSettlement)替代 `settled_amount>0` 守"禁改票面/禁删"。列表按钮 `can_settle_ar`(票面−已抵应收>0) 与 `can_endorse`(未用>0) 分别判。**数据迁移 0019**：把历史应收票据 settled_amount 重算为"仅背书合计"、状态重置——三家应收票据余额会上升(把本就持有的票补回账)，应收账款不变。受影响旧测试已按新口径更新(核销应收→票在手/未用满)。
15. **撤销票据冲销（2026-06-17）**：`reverse_note_settlement` —— 反向一笔 `NoteSettlement`：发票 `settled_amount -= s.amount`、票据 `settled_amount -= s.amount`、状态恢复（应收→在手 ON_HAND、应付→已开出 ISSUED，撤销后必有未用），删除该冲销记录、记 `AuditLog.OFFSET`。入口：发票详情「核销明细」票据行 + 票据使用明细每笔使用都加「撤销」按钮（`_invoice_settlements`/`note_ledger` 带出 `settlement_id`）。`require_POST` + confirm + `next` 安全回跳（`url_has_allowed_host_and_scheme`）。权限沿用 `add_notesettlement`。**背景**：用户指出"收到客户票据抵货款被记成'核销应收账款'并把票标已用，等于把持有的票从账上抹掉"——确认正确口径应为**借应收票据/贷应收账款，票留持有不消耗**（待办：改"核销应收不消耗票"的口径＋历史数据迁移，本次先做撤销救数据）。
14. **应收票据「已用」可点查使用明细（2026-06-17）**：列表「已用」>0 时链到 `receivable_note_ledger?company=&note=&all=1`。`note_ledger` 本按日期区间过滤、默认本月，早月用掉的票其使用事件会折进"期初"不单显——故 ledger 视图加 `?all=1`（dfrom=1900-01-01、dto=today）看全部。排查"已用查无记录"的根因：使用记录(`NoteSettlement`+`AuditLog.OFFSET`)只由冲应收/背书产生且必同时落库；导入只建票不写已用。所以已用>0 必有使用记录，只是列表页不展示，需进使用明细。
13. **应收票据「删除」（2026-06-17）**：`delete_note_receivable` + `note_receivable_delete_block_reason`——**只要「未使用(settled_amount==0)」即可删（含期初票据）**。未用票据不挂 AR/AP、无银行日记账、无镜像，删除干净；期初票据正是导入时最易录错、最需删的，故**不拦期初**（初版曾加「非期初」护栏，导致生产上全是「已用 6/11 票 + 期初 6/1 票」、删除按钮一个都不显示——放开期初后未用期初票即可删）。已使用的票据删了会留孤儿 NoteSettlement（其用 `note_id` 泛指引用、无外键级联），故硬拦。入口：资金▸应收票据列表 + 收款统一一览的票据行「修改」旁加「删除」；require_POST + confirm。沿用 `add_notereceivable` 权限。
12. **账户余额表分组合计（2026-06-12）**：四个分组（银行/应收/应付/库存）网页 tfoot + Excel 导出各加「合计」行；视图统一算 `block["total"]`，模板/导出共用。
13. **应收票据「修改」补录（2026-06-17）**：`update_note_receivable` + `note_receivable_edit_block_reason`（仅作废不可改；已使用则票面金额锁定、描述字段仍可补录——`NoteReceivableForm` 在 `instance.settled_amount>0` 时 `amount.disabled=True`）。入口：应收票据列表 + 收款/付款统一一览的票据行（`_receipt_rows/_payment_rows` 给票据行补 `edit_url`，原先 `can_edit=False`）。权限沿用 `add_notereceivable`（零权限迁移）。
13. **统一收/付一览（已完成）**：收款/付款列表「银行账户」列改「收款/付款方式」，**合并两类数据源**——
    - 列表视图由 ClassView 改为**函数视图** `receipt_list / payment_list`（`apps/finance/views.py`）；`_receipt_rows / _payment_rows` 产出统一行 dict，`_cash_list_filter` 做日期+关键字过滤、`_export_cash_rows` 导出。
    - 收款行 = `Receipt`(银行) + `NoteReceivable`(应收票据)；付款行 = `Payment`(银行) + `NoteSettlement` 背书(按票据 `note_no` 归并、汇总供应商)。
    - 票据行只读（无修改/删除，操作在「资金▸应收票据」），方式列带蓝色 badge。保留筛选+Excel 导出。
    - 已删除 `PaymentListView/ReceiptListView` 类，URL 指向函数视图。

### 三、已知问题 / 临时凑合（下次回头看）
- ~~**死代码**：`InsufficientStockError` 类自负库存放开后已无处抛出；`apps/sales/views.py` 的对应 `except` 成无害死代码（可清理）。~~ **已清理（2026-07-03）**：删除 `InsufficientStockError` 类 + `sales/views.py` 两处 `except` + 各处 import/docstring 引用。
- ~~**核销无落库日期，用 `created_at.date()` 近似（已知限制）**~~ **已修（2026-07-03，迁移 `0021`）**：`PaymentAllocation/ReceiptAllocation/NoteSettlement` 各加 `date` 业务日期（核销取付款/收款 doc_date、票据冲销取 draw_date），报表改按 `date` 归期，跨月核销不再错记到操作月。此前依赖系统日期的 7 个测试随之转绿。
- **付款一览「应收票据背书」归并口径偏简化**：按票据 `note_no` 归并求和；若一张票分多次背书给不同供应商，会显示"N 个供应商"，日期取**票据出票日**（非背书日）。够看不够精细，必要时可改为按背书批次/日期拆行。
- ~~**销售出库还没有「删除」按钮**（只有作废）；采购入库已加受限硬删除。对称性待补~~ **已补（2026-06-30）**：`delete_sales_outbound` + `outbound_delete_block_reason` 完全对称采购入库受限硬删除——仅「该出库后该商品再无任何出入库变动 + 未生成关联镜像 + 未开票 + 当月 + 本人/管理员」才允许，`reverse_move` 精确反冲恢复库存再删两条流水不留痕（顺带清 ExpenseEntry/BorrowTransaction）；其余用「作废」。列表/详情按 `can_delete` 显隐，URL `outbound_delete`。新增 5 个测试（`OutboundDeleteTests`）。
- **DB 状态偏差（本轮已修文档）**：CLAUDE.md/DEPLOY.md 原写生产用 PostgreSQL，**实际生产跑的是 SQLite**（代码两者都支持，靠 `DB_ENGINE` 切换）。已在本轮把文档改为"实际 SQLite"。

### 四、下一步建议从哪继续
- **发出商品明细表（2026-07-15）**：总览/账户余额「发出商品」下钻改为 `goods_shipped_detail_report`（按出库行列期初/本期收入/本期发出/期末，含已全部开票行），与总览四列同口径；原「已出库未开票明细表」仍保留在报表菜单，专查未开票余额。
- **期初模板含发出商品/应付暂估（2026-07-15）**：下载模板新增 sheet「期初发出商品」「期初应付账款-暂估」；导入建 `is_opening` 出库/入库台账（不重复加减库存），总览期初恒计入；可分类清空。迁移 `sales.0006` / `purchasing.0005`。
- **M18 订单主线 SPEC（2026-07-15）**：`SPEC.md` §3 流程改为订单目标主线 + 过渡期；新增 **§20**（销售/采购订单、行级统计与状态、保留出库↔发票匹配、模式 A、已完成不补/未完成回挂不改入账、M18-1~5 切片）。下一步 M18-2 实施销售订单。
- **销售成本计算单打印（2026-07-14）**：销售出库详情「打印」旁加「销售成本计算单」按钮（需 `inventory.view_amount`）；新页 `outbound_cost_print` 按本单列示销售数量、不含税/含税金额、结转成本，便于按单结转成本核对。无迁移。
- **M17 财务管理（2026-07-14）**：菜单「财务管理」→ 往来对冲 / 票据拆借。SPEC §7.5–§7.6。往来对冲：`PartnerOffset` 互抵 AR/AP 发票 `settled_amount`；票据拆借：应收票跨公司转移 + `IntercoBalance` 其他应收/其他应付·关联方。应付票据拆借尚未做。
- 本项目无固定 backlog，处于**用户驱动按需迭代**。下次大概率是用户截图提新需求/报 bug。
- **改完务必**：`uv run python manage.py test` 全绿 + 重生成 `docs/操作手册.docx/.pdf`（脚本：`uv run --with python-docx python docs/_md2docx.py …`；PDF 走 `_md2html.py` + headless Chrome 打印，命令见近期 commit）。
