"""商品 / 客户 / 供应商 的列表与增删改（基于 core.crud 通用基类，公司维度过滤）。"""

from django.urls import reverse_lazy

from apps.core.crud import (
    ScopedCreateView,
    ScopedDeleteView,
    ScopedListView,
    ScopedUpdateView,
)

from .forms import (
    BusinessPartnerForm, CustomerForm, ExpenseCategoryForm, ProductForm, SupplierForm,
)
from .models import (
    BusinessPartner, Customer, ExpenseCategory, InvoiceQuota, Product, Supplier,
)


# --- 商品 ---------------------------------------------------------------------
class ProductListView(ScopedListView):
    model = Product
    title = "商品"
    columns = [("编码", "code"), ("名称", "name"), ("规格", "spec"),
               ("单位", "unit"), ("分类", "category"), ("启用", "is_active")]
    search_fields = ["code", "name", "spec", "category"]
    q_placeholder = "编码/名称/规格"
    create_url_name = "product_create"
    update_url_name = "product_update"
    delete_url_name = "product_delete"


class ProductCreateView(ScopedCreateView):
    model = Product
    form_class = ProductForm
    title = "商品"
    success_url = reverse_lazy("product_list")


class ProductUpdateView(ScopedUpdateView):
    model = Product
    form_class = ProductForm
    title = "商品"
    success_url = reverse_lazy("product_list")


class ProductDeleteView(ScopedDeleteView):
    model = Product
    title = "商品"
    success_url = reverse_lazy("product_list")


# --- 往来单位（SPEC §21）-------------------------------------------------------
class PartnerListView(ScopedListView):
    model = BusinessPartner
    title = "往来单位"
    columns = [("编码", "code"), ("名称", "name"),
               ("客户", "is_customer"), ("供应商", "is_supplier"),
               ("电话", "phone"), ("关联企业", "related_company"), ("启用", "is_active")]
    search_fields = ["code", "name", "contact", "phone", "tax_no"]
    q_placeholder = "编码/名称/税号"
    create_url_name = "partner_create"
    update_url_name = "partner_update"
    delete_url_name = "partner_delete"
    import_url_name = "partner_import"

    def get_queryset(self):
        qs = super().get_queryset()
        role = self.request.GET.get("role") or ""
        if role == "customer":
            qs = qs.filter(is_customer=True)
        elif role == "supplier":
            qs = qs.filter(is_supplier=True)
        return qs


class PartnerCreateView(ScopedCreateView):
    model = BusinessPartner
    form_class = BusinessPartnerForm
    title = "往来单位"
    success_url = reverse_lazy("partner_list")


class PartnerUpdateView(ScopedUpdateView):
    model = BusinessPartner
    form_class = BusinessPartnerForm
    title = "往来单位"
    success_url = reverse_lazy("partner_list")


class PartnerDeleteView(ScopedDeleteView):
    model = BusinessPartner
    title = "往来单位"
    success_url = reverse_lazy("partner_list")


# --- 往来单位 Excel 导入 -------------------------------------------------------
from django.contrib import messages  # noqa: E402
from django.contrib.auth.decorators import login_required, permission_required  # noqa: E402
from django.http import HttpResponse  # noqa: E402
from django.shortcuts import redirect, render  # noqa: E402

from apps.core.scope import get_active_company, get_visible_companies  # noqa: E402


@login_required
@permission_required("masterdata.add_businesspartner", raise_exception=True)
def partner_import(request):
    """Excel 导入往来单位（按编码 upsert）。"""
    from .excel import import_partners, parse_partners_xlsx

    company = get_active_company(request, list(get_visible_companies(request.user)))
    if request.method == "POST":
        upload = request.FILES.get("file")
        if not company:
            messages.error(request, "没有可用的当前账套")
        elif not upload:
            messages.error(request, "请上传 Excel 文件（.xlsx）")
        else:
            try:
                rows, parse_errors = parse_partners_xlsx(upload)
            except Exception as e:
                messages.error(request, f"文件解析失败：{e}")
            else:
                created, updated, import_errors = import_partners(
                    company=company, user=request.user, rows=rows)
                errors = parse_errors + import_errors
                msg = f"导入完成：新增 {created} 条，更新 {updated} 条"
                if errors:
                    msg += f"；{len(errors)} 行有问题"
                messages.success(request, msg)
                for e in errors[:15]:
                    messages.warning(request, e)
                return redirect("partner_list")
    return render(request, "masterdata/partner_import.html", {"company": company})


@login_required
@permission_required("masterdata.add_businesspartner", raise_exception=True)
def partner_template(request):
    """下载往来单位导入模板。"""
    from .excel import build_partner_template

    data = build_partner_template()
    resp = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resp["Content-Disposition"] = 'attachment; filename="往来单位导入模板.xlsx"'
    return resp


# --- 客户（代理兼容入口，列表实为 is_customer）--------------------------------
class CustomerListView(ScopedListView):
    model = Customer
    title = "客户"
    columns = [("编码", "code"), ("名称", "name"), ("联系人", "contact"),
               ("电话", "phone"), ("关联企业", "related_company"), ("启用", "is_active")]
    search_fields = ["code", "name", "contact", "phone"]
    q_placeholder = "编码/名称/联系人"
    create_url_name = "customer_create"
    update_url_name = "customer_update"
    delete_url_name = "customer_delete"


class CustomerCreateView(ScopedCreateView):
    model = Customer
    form_class = CustomerForm
    title = "客户"
    success_url = reverse_lazy("customer_list")


class CustomerUpdateView(ScopedUpdateView):
    model = Customer
    form_class = CustomerForm
    title = "客户"
    success_url = reverse_lazy("customer_list")


class CustomerDeleteView(ScopedDeleteView):
    model = Customer
    title = "客户"
    success_url = reverse_lazy("customer_list")


# --- 供应商 -------------------------------------------------------------------
class SupplierListView(ScopedListView):
    model = Supplier
    title = "供应商"
    columns = [("编码", "code"), ("名称", "name"), ("联系人", "contact"),
               ("电话", "phone"), ("关联企业", "related_company"), ("启用", "is_active")]
    search_fields = ["code", "name", "contact", "phone"]
    q_placeholder = "编码/名称/联系人"
    create_url_name = "supplier_create"
    update_url_name = "supplier_update"
    delete_url_name = "supplier_delete"


class SupplierCreateView(ScopedCreateView):
    model = Supplier
    form_class = SupplierForm
    title = "供应商"
    success_url = reverse_lazy("supplier_list")


class SupplierUpdateView(ScopedUpdateView):
    model = Supplier
    form_class = SupplierForm
    title = "供应商"
    success_url = reverse_lazy("supplier_list")


class SupplierDeleteView(ScopedDeleteView):
    model = Supplier
    title = "供应商"
    success_url = reverse_lazy("supplier_list")


# --- 费用类别（M6 其他费用）---------------------------------------------------
class ExpenseCategoryListView(ScopedListView):
    model = ExpenseCategory
    title = "费用类别"
    columns = [("费用类别", "name"), ("计入存货成本", "include_in_cost"), ("启用", "is_active")]
    search_fields = ["name"]
    q_placeholder = "费用类别"
    create_url_name = "expensecategory_create"
    update_url_name = "expensecategory_update"
    delete_url_name = "expensecategory_delete"


class ExpenseCategoryCreateView(ScopedCreateView):
    model = ExpenseCategory
    form_class = ExpenseCategoryForm
    title = "费用类别"
    success_url = reverse_lazy("expensecategory_list")


class ExpenseCategoryUpdateView(ScopedUpdateView):
    model = ExpenseCategory
    form_class = ExpenseCategoryForm
    title = "费用类别"
    success_url = reverse_lazy("expensecategory_list")


class ExpenseCategoryDeleteView(ScopedDeleteView):
    model = ExpenseCategory
    title = "费用类别"
    success_url = reverse_lazy("expensecategory_list")


# --- 一键复制基础资料到其他公司（仅管理员）-----------------------------------
def copy_masterdata(request):
    """把当前账套的 商品/客户/供应商/费用类别 复制到其他公司。

    以右上「当前账套」为源；目标公司已存在相同编码(费用类别按名称)的自动跳过，
    故可反复执行、幂等安全。仅复制主数据本身，不动库存与单据。
    """
    from django.contrib import messages
    from django.core.exceptions import PermissionDenied
    from django.db import transaction
    from django.shortcuts import redirect, render

    from apps.core.scope import get_active_company, get_visible_companies

    if not request.user.is_authenticated:
        return redirect("login")
    if not request.user.is_superuser:
        raise PermissionDenied("仅管理员可使用基础资料复制")

    visible = list(get_visible_companies(request.user))
    source = get_active_company(request, visible)
    others = [c for c in visible if source and c.pk != source.pk]

    # key, 中文, 模型, 判重字段, 复制字段
    type_defs = [
        ("product", "商品", Product, "code",
         ["code", "name", "spec", "unit", "category", "default_tax_rate", "is_active", "remark"]),
        ("customer", "客户", Customer, "code",
         ["code", "name", "contact", "phone", "tax_no", "address", "is_active", "remark"]),
        ("supplier", "供应商", Supplier, "code",
         ["code", "name", "contact", "phone", "tax_no", "address", "is_active", "remark"]),
        ("expensecategory", "费用类别", ExpenseCategory, "name",
         ["name", "include_in_cost", "is_active", "remark"]),
    ]
    ui_types = [{"key": k, "label": l, "count": M.objects.filter(company=source).count() if source else 0}
                for (k, l, M, _kf, _fs) in type_defs]

    results = None
    if request.method == "POST":
        target_ids = set(request.POST.getlist("targets"))
        chosen_types = set(request.POST.getlist("types"))
        targets = [c for c in others if str(c.pk) in target_ids]
        if not source:
            messages.error(request, "没有可用的当前账套")
            return redirect("masterdata_copy")
        if not targets or not chosen_types:
            messages.error(request, "请至少选择一个目标公司和一类基础资料")
            return redirect("masterdata_copy")
        results = []
        with transaction.atomic():
            for key, label, Model, keyf, fields in type_defs:
                if key not in chosen_types:
                    continue
                src_rows = list(Model.objects.filter(company=source))
                is_partner = key in ("customer", "supplier")
                for tgt in targets:
                    existing = set(Model.objects.filter(company=tgt).values_list(keyf, flat=True))
                    created = skipped = 0
                    for row in src_rows:
                        kv = getattr(row, keyf)
                        if kv in existing:
                            skipped += 1
                            continue
                        data = {f: getattr(row, f) for f in fields}
                        data["company"] = tgt
                        if is_partner:
                            rc = row.related_company_id
                            # 关联企业指向目标本身时不带（公司不能是自己的往来对象）
                            data["related_company_id"] = None if rc == tgt.pk else rc
                        Model.objects.create(**data)
                        created += 1
                    results.append({"label": label, "target": str(tgt),
                                    "created": created, "skipped": skipped})
        total = sum(r["created"] for r in results)
        messages.success(request, f"复制完成：共新建 {total} 条（已存在的同编码已跳过）。")

    return render(request, "masterdata/copy.html", {
        "source": source, "others": others, "ui_types": ui_types, "results": results,
    })


# --- 开具发票额度录入（按公司·月份）-----------------------------------------
def invoice_quota(request):
    """录入/查看三家公司每月可开具发票额度。"""
    from decimal import Decimal, InvalidOperation

    from django.contrib import messages
    from django.core.exceptions import PermissionDenied
    from django.shortcuts import redirect, render

    from apps.core.scope import get_visible_companies

    if not request.user.is_authenticated:
        return redirect("login")
    can_view = request.user.is_superuser or request.user.has_perm("finance.view_salesinvoice")
    if not can_view:
        raise PermissionDenied("无权查看开票额度")
    can_edit = request.user.is_superuser or request.user.has_perm("finance.add_salesinvoice")
    visible = list(get_visible_companies(request.user))

    if request.method == "POST":
        if not can_edit:
            raise PermissionDenied("无权修改开票额度")
        if request.POST.get("action") == "delete":
            InvoiceQuota.objects.filter(pk=request.POST.get("id"), company__in=visible).delete()
            messages.success(request, "已删除该额度。")
            return redirect("invoice_quota")
        cid = request.POST.get("company")
        company = next((c for c in visible if str(c.pk) == cid), None)
        try:
            amt = Decimal(request.POST.get("amount") or "")
        except (InvalidOperation, TypeError):
            amt = None
        if not company:
            messages.error(request, "请选择公司")
        elif amt is None:
            messages.error(request, "请输入有效金额")
        else:
            obj, created = InvoiceQuota.objects.update_or_create(
                company=company,
                defaults={"amount": amt, "remark": request.POST.get("remark", "")})
            messages.success(request, f"{'已新增' if created else '已更新'} {company} 每月开票额度 {amt}")
        return redirect("invoice_quota")

    rows = (InvoiceQuota.objects.filter(company__in=visible)
            .select_related("company").order_by("company__code"))
    return render(request, "masterdata/invoice_quota.html", {
        "rows": rows, "companies": visible, "can_edit": can_edit})
