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
