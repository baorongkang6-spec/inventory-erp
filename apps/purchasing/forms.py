"""采购入库录入表单：单头 + 多行明细 formset。

product/supplier 下拉按当前账套过滤。空行（未填商品）跳过，至少要有一行有效明细。
"""

from django import forms

from apps.core.forms import BootstrapForm
from apps.core.money import DEFAULT_TAX_RATE
from apps.masterdata.models import Product, Supplier

from .models import PurchaseInbound, PurchaseOrder


class InboundHeaderForm(BootstrapForm):
    doc_date = forms.DateField(label="入库日期")
    purchase_type = forms.ChoiceField(label="采购方式", choices=PurchaseInbound.PurchaseType.choices)
    supplier = forms.ModelChoiceField(
        label="供应商/出借方", queryset=Supplier.objects.none(), required=False, empty_label="（外购/未指定）"
    )
    purchase_order = forms.ModelChoiceField(
        label="采购订单", queryset=PurchaseOrder.objects.none(), required=False,
        empty_label="（不关联订单）",
    )
    remark = forms.CharField(label="备注", required=False, max_length=255)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["supplier"].queryset = Supplier.objects.filter(
                company=company, is_active=True
            )
            pks = list(PurchaseOrder.objects.filter(
                company=company, status=PurchaseOrder.Status.OPEN
            ).values_list("pk", flat=True))
            cur = self.initial.get("purchase_order")
            if cur is not None:
                pk = cur.pk if isinstance(cur, PurchaseOrder) else int(cur)
                if pk not in pks:
                    pks.append(pk)
            self.fields["purchase_order"].queryset = (
                PurchaseOrder.objects.filter(company=company, pk__in=pks)
                .select_related("supplier").order_by("-doc_date", "-id")
            )

    def clean(self):
        cleaned = super().clean()
        order = cleaned.get("purchase_order")
        supplier = cleaned.get("supplier")
        purchase_type = cleaned.get("purchase_type")
        if purchase_type == PurchaseInbound.PurchaseType.PURCHASE_RETURN and order:
            self.add_error("purchase_order", "采购退回不关联采购订单（不计入订单收货进度）")
        if order and supplier and order.supplier_id != supplier.pk:
            self.add_error("supplier", f"须与订单供应商一致（{order.supplier}）")
        if order and not supplier:
            cleaned["supplier"] = order.supplier
        return cleaned


class InboundLineForm(BootstrapForm):
    product = forms.ModelChoiceField(
        label="商品", queryset=Product.objects.none(), required=False, empty_label="—"
    )
    quantity = forms.DecimalField(label="数量", required=False, max_digits=18, decimal_places=3, min_value=0)
    tax_inclusive_price = forms.DecimalField(label="含税单价", required=False, max_digits=18,
                                             decimal_places=2, min_value=0)
    tax_rate = forms.DecimalField(label="税率", required=False, max_digits=5, decimal_places=4,
                                  min_value=0, max_value=1, initial=DEFAULT_TAX_RATE)
    amount_taxed = forms.DecimalField(label="含税金额", required=False, max_digits=18, decimal_places=2, min_value=0)
    amount_untaxed = forms.DecimalField(label="不含税金额", required=False, max_digits=18, decimal_places=2, min_value=0)
    tax_amount = forms.DecimalField(label="税额", required=False, max_digits=18, decimal_places=2, min_value=0)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["product"].queryset = Product.objects.filter(
                company=company, is_active=True
            )

    def clean(self):
        cleaned = super().clean()
        product = cleaned.get("product")
        qty = cleaned.get("quantity")
        tip = cleaned.get("tax_inclusive_price")
        untaxed = cleaned.get("amount_untaxed")
        taxed = cleaned.get("amount_taxed")
        # 空行（完全没填）→ 标记跳过
        if not product and not qty and tip is None and untaxed is None and taxed is None:
            cleaned["_empty"] = True
            return cleaned
        cleaned["_empty"] = False
        if not product:
            self.add_error("product", "请选择商品")
        if qty is None or qty <= 0:
            self.add_error("quantity", "数量必须大于 0")
        if tip is None and untaxed is None and taxed is None:
            self.add_error("tax_inclusive_price", "请填写含税单价（或金额）")
        if cleaned.get("tax_rate") is None:
            cleaned["tax_rate"] = DEFAULT_TAX_RATE
        return cleaned


class BaseInboundLineFormSet(forms.BaseFormSet):
    def __init__(self, *args, company=None, **kwargs):
        self.company = company
        super().__init__(*args, **kwargs)

    def get_form_kwargs(self, index):
        kwargs = super().get_form_kwargs(index)
        kwargs["company"] = self.company
        return kwargs

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        valid_lines = [
            f.cleaned_data for f in self.forms
            if f.cleaned_data and not f.cleaned_data.get("_empty")
        ]
        if not valid_lines:
            raise forms.ValidationError("至少录入一行明细")
        self.valid_lines = valid_lines


InboundLineFormSet = forms.formset_factory(
    InboundLineForm, formset=BaseInboundLineFormSet, extra=3
)


class PurchaseOrderHeaderForm(BootstrapForm):
    doc_date = forms.DateField(label="订单日期")
    supplier = forms.ModelChoiceField(
        label="供应商", queryset=Supplier.objects.none(), empty_label="请选择供应商"
    )
    remark = forms.CharField(label="备注", required=False, max_length=255)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["supplier"].queryset = Supplier.objects.filter(
                company=company, is_active=True
            )


class PurchaseOrderLineForm(InboundLineForm):
    pass


class BasePurchaseOrderLineFormSet(BaseInboundLineFormSet):
    pass


PurchaseOrderLineFormSet = forms.formset_factory(
    PurchaseOrderLineForm, formset=BasePurchaseOrderLineFormSet, extra=3
)


class PurchaseOrderReceiveForm(BootstrapForm):
    doc_date = forms.DateField(label="入库日期")
    remark = forms.CharField(label="备注", required=False, max_length=255)


class PurchaseOrderInvoiceForm(BootstrapForm):
    doc_date = forms.DateField(label="收票日期")
    invoice_no = forms.CharField(label="发票号码", required=False, max_length=64)
    term_days = forms.IntegerField(label="账期(天)", required=False, min_value=0, initial=0)
    remark = forms.CharField(label="备注", required=False, max_length=255)
