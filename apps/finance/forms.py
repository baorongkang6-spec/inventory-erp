"""资金往来表单。"""

from django import forms

from apps.core.forms import BootstrapForm, CompanyScopedModelForm
from apps.core.money import DEFAULT_TAX_RATE
from apps.masterdata.models import Customer, Product, Supplier

from .models import BankAccount, BankJournal, NotePayable, NoteReceivable, Payment, Receipt


class OtherCashflowForm(forms.Form):
    """其他收支登记（M8-2）：非往来的银行收/支，直接成日记账。"""

    doc_date = forms.DateField(label="日期",
                               widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    bank_account = forms.ModelChoiceField(label="银行账户", queryset=BankAccount.objects.none())
    direction = forms.ChoiceField(label="方向", choices=BankJournal.Direction.choices)
    # 排除「往来结算」——那应走付款/收款登记
    entry_type = forms.ChoiceField(
        label="业务类型",
        choices=[c for c in BankJournal.EntryType.choices
                 if c[0] != BankJournal.EntryType.SETTLEMENT])
    amount = forms.DecimalField(label="金额", max_digits=18, decimal_places=2, min_value=0)
    counterparty = forms.CharField(label="对方单位", max_length=128, required=False)
    summary = forms.CharField(label="摘要", max_length=255, required=False)
    txn_no = forms.CharField(label="交易流水号", max_length=64, required=False)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["bank_account"].queryset = BankAccount.objects.filter(
                company=company, is_active=True)
        for name, field in self.fields.items():
            w = field.widget
            w.attrs.setdefault("class", "form-select" if isinstance(w, forms.Select) else "form-control")

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("金额必须大于 0")
        return amount


class BankAccountForm(CompanyScopedModelForm):
    class Meta:
        model = BankAccount
        fields = ["name", "bank_name", "account_no", "opening_balance", "is_active", "remark"]


# --- 采购发票 -----------------------------------------------------------------
class PurchaseInvoiceHeaderForm(BootstrapForm):
    doc_date = forms.DateField(label="开票日期")
    term_days = forms.IntegerField(label="账期(天)", required=False, min_value=0, initial=0,
                                   help_text="0=即期；到期日=开票日期+账期")
    supplier = forms.ModelChoiceField(label="供应商", queryset=Supplier.objects.none())
    invoice_no = forms.CharField(label="发票号码", required=False, max_length=64)
    remark = forms.CharField(label="备注", required=False, max_length=255)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["supplier"].queryset = Supplier.objects.filter(
                company=company, is_active=True
            )


class PurchaseInvoiceLineForm(BootstrapForm):
    product = forms.ModelChoiceField(
        label="商品", queryset=Product.objects.none(), required=False, empty_label="—（可不选）"
    )
    description = forms.CharField(label="摘要", required=False, max_length=128)
    quantity = forms.DecimalField(label="数量", required=False, max_digits=18, decimal_places=3)
    amount_untaxed = forms.DecimalField(
        label="不含税金额", required=False, max_digits=18, decimal_places=2
    )
    tax_rate = forms.DecimalField(
        label="税率", required=False, max_digits=5, decimal_places=4,
        min_value=0, max_value=1, initial=DEFAULT_TAX_RATE,
    )
    tax_amount = forms.DecimalField(
        label="税额", required=False, max_digits=18, decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control text-end", "step": "0.01"}),
    )
    amount_taxed = forms.DecimalField(
        label="含税金额", required=False, max_digits=18, decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control text-end", "step": "0.01"}),
    )
    # 关联入库行 id（隐藏，「从入库单带入」时写入；用于暂估匹配）
    source_inbound_line = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["product"].queryset = Product.objects.filter(
                company=company, is_active=True
            )

    def clean(self):
        cleaned = super().clean()
        amt = cleaned.get("amount_untaxed")
        product = cleaned.get("product")
        desc = cleaned.get("description")
        if amt in (None, "") and not product and not desc:
            cleaned["_empty"] = True
            return cleaned
        cleaned["_empty"] = False
        if amt is None:
            self.add_error("amount_untaxed", "请输入不含税金额（红冲/退货可填负数）")
        elif amt == 0:
            self.add_error("amount_untaxed", "金额不能为 0")
        if cleaned.get("tax_rate") is None:
            cleaned["tax_rate"] = DEFAULT_TAX_RATE
        return cleaned


class BasePurchaseInvoiceLineFormSet(forms.BaseFormSet):
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
        valid = [f.cleaned_data for f in self.forms
                 if f.cleaned_data and not f.cleaned_data.get("_empty")]
        if not valid:
            raise forms.ValidationError("至少录入一行明细")
        self.valid_lines = valid


PurchaseInvoiceLineFormSet = forms.formset_factory(
    PurchaseInvoiceLineForm, formset=BasePurchaseInvoiceLineFormSet, extra=3
)


# --- 付款登记 -----------------------------------------------------------------
class PaymentForm(forms.ModelForm):
    """付款登记。付款方式 = 各银行账户 + 应收票据(背书)；company 由视图注入。

    - 选银行账户：照常生成银行日记账（支出）。
    - 选「应收票据(背书)」：用手上一张在手应收票据背书抵付应付（不生成银行日记账）。
    """

    METHOD_NOTE = "note"

    method = forms.ChoiceField(label="付款方式", choices=[])
    note_no = forms.CharField(label="票据号码", required=False, max_length=64,
                              help_text="背书时填在手应收票据号，自动带出出票/到期/余额")

    class Meta:
        model = Payment
        fields = ["doc_date", "supplier", "amount", "summary"]
        widgets = {"doc_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        accounts = []
        if company is not None:
            accounts = list(BankAccount.objects.filter(company=company, is_active=True))
            self.fields["supplier"].queryset = Supplier.objects.filter(
                company=company, is_active=True
            )
        self.fields["method"].choices = (
            [(f"bank:{a.pk}", f"银行账户 · {a.name}") for a in accounts]
            + [(self.METHOD_NOTE, "应收票据（背书）")]
        )
        self.fields["supplier"].required = False
        self.fields["supplier"].empty_label = "（其他付款，可不选）"
        for name, field in self.fields.items():
            w = field.widget
            w.attrs.setdefault("class", "form-select" if isinstance(w, forms.Select) else "form-control")

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("付款金额必须大于 0")
        return amount

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("method") or ""
        if method == self.METHOD_NOTE:
            cleaned["bank_account"] = None
            if not cleaned.get("supplier"):
                self.add_error("supplier", "背书付款时，收款供应商必填")
            if not cleaned.get("note_no"):
                self.add_error("note_no", "请填写要背书的应收票据号码")
        elif method.startswith("bank:"):
            try:
                acc_id = int(method.split(":", 1)[1])
                cleaned["bank_account"] = BankAccount.objects.get(pk=acc_id, company=self.company)
            except (ValueError, BankAccount.DoesNotExist):
                self.add_error("method", "请选择有效的付款方式")
        else:
            self.add_error("method", "请选择付款方式")
        return cleaned


# --- 销售发票 -----------------------------------------------------------------
class SalesInvoiceHeaderForm(BootstrapForm):
    doc_date = forms.DateField(label="开票日期")
    term_days = forms.IntegerField(label="账期(天)", required=False, min_value=0, initial=0,
                                   help_text="0=即期；到期日=开票日期+账期")
    customer = forms.ModelChoiceField(label="客户", queryset=Customer.objects.none())
    invoice_no = forms.CharField(label="发票号码", required=False, max_length=64)
    remark = forms.CharField(label="备注", required=False, max_length=255)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["customer"].queryset = Customer.objects.filter(
                company=company, is_active=True
            )


class SalesInvoiceLineForm(BootstrapForm):
    product = forms.ModelChoiceField(
        label="商品", queryset=Product.objects.none(), required=False, empty_label="—（可不选）"
    )
    description = forms.CharField(label="摘要", required=False, max_length=128)
    quantity = forms.DecimalField(label="数量", required=False, max_digits=18, decimal_places=3)
    amount_untaxed = forms.DecimalField(
        label="不含税金额", required=False, max_digits=18, decimal_places=2
    )
    tax_rate = forms.DecimalField(
        label="税率", required=False, max_digits=5, decimal_places=4,
        min_value=0, max_value=1, initial=DEFAULT_TAX_RATE,
    )
    tax_amount = forms.DecimalField(
        label="税额", required=False, max_digits=18, decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control text-end", "step": "0.01"}),
    )
    amount_taxed = forms.DecimalField(
        label="含税金额", required=False, max_digits=18, decimal_places=2,
        widget=forms.NumberInput(attrs={"class": "form-control text-end", "step": "0.01"}),
    )
    # 关联出库行 id（隐藏，「从出库单带入」时写入；用于成本匹配）
    source_outbound_line = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["product"].queryset = Product.objects.filter(
                company=company, is_active=True
            )

    def clean(self):
        cleaned = super().clean()
        amt = cleaned.get("amount_untaxed")
        product = cleaned.get("product")
        desc = cleaned.get("description")
        if amt in (None, "") and not product and not desc:
            cleaned["_empty"] = True
            return cleaned
        cleaned["_empty"] = False
        if amt is None:
            self.add_error("amount_untaxed", "请输入不含税金额（红冲/退货可填负数）")
        elif amt == 0:
            self.add_error("amount_untaxed", "金额不能为 0")
        if cleaned.get("tax_rate") is None:
            cleaned["tax_rate"] = DEFAULT_TAX_RATE
        return cleaned


class BaseSalesInvoiceLineFormSet(forms.BaseFormSet):
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
        valid = [f.cleaned_data for f in self.forms
                 if f.cleaned_data and not f.cleaned_data.get("_empty")]
        if not valid:
            raise forms.ValidationError("至少录入一行明细")
        self.valid_lines = valid


SalesInvoiceLineFormSet = forms.formset_factory(
    SalesInvoiceLineForm, formset=BaseSalesInvoiceLineFormSet, extra=3
)


# --- 收款登记 -----------------------------------------------------------------
class ReceiptForm(forms.ModelForm):
    """收款登记。收款方式 = 各银行账户 + 应收票据；company 由视图注入。

    - 选银行账户：照常生成银行日记账（收入）。
    - 选「应收票据」：收到客户票据，生成一张在手应收票据（不生成银行日记账），
      可在同界面勾选要冲抵的销售发票（冲应收）。
    """

    METHOD_NOTE = "note"

    method = forms.ChoiceField(label="收款方式", choices=[])
    note_no = forms.CharField(label="票据号码", required=False, max_length=64)
    draw_date = forms.DateField(label="出票日期", required=False,
                                widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    due_date = forms.DateField(label="到期日期", required=False,
                               widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))

    class Meta:
        model = Receipt
        fields = ["doc_date", "customer", "amount", "summary"]
        widgets = {"doc_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.company = company
        accounts = []
        if company is not None:
            accounts = list(BankAccount.objects.filter(company=company, is_active=True))
            self.fields["customer"].queryset = Customer.objects.filter(
                company=company, is_active=True
            )
        self.fields["method"].choices = (
            [(f"bank:{a.pk}", f"银行账户 · {a.name}") for a in accounts]
            + [(self.METHOD_NOTE, "应收票据")]
        )
        self.fields["customer"].required = False
        self.fields["customer"].empty_label = "（其他收款，可不选）"
        for name, field in self.fields.items():
            w = field.widget
            if isinstance(w, forms.DateInput):
                w.attrs.setdefault("type", "date")
            w.attrs.setdefault("class", "form-select" if isinstance(w, forms.Select) else "form-control")

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("收款金额必须大于 0")
        return amount

    def clean(self):
        cleaned = super().clean()
        method = cleaned.get("method") or ""
        if method == self.METHOD_NOTE:
            cleaned["bank_account"] = None
            if not cleaned.get("customer"):
                self.add_error("customer", "收款方式为应收票据时，付款客户必填")
            if not cleaned.get("note_no"):
                self.add_error("note_no", "请填写票据号码")
            if not cleaned.get("draw_date"):
                self.add_error("draw_date", "请填写出票日期")
            if not cleaned.get("due_date"):
                self.add_error("due_date", "请填写到期日期")
        elif method.startswith("bank:"):
            try:
                acc_id = int(method.split(":", 1)[1])
                cleaned["bank_account"] = BankAccount.objects.get(pk=acc_id, company=self.company)
            except (ValueError, BankAccount.DoesNotExist):
                self.add_error("method", "请选择有效的收款方式")
        else:
            self.add_error("method", "请选择收款方式")
        return cleaned


# --- 收款/付款修改（仅银行方式单据）------------------------------------------
class ReceiptEditForm(forms.ModelForm):
    """修改收款：方式固定为银行，仅改日期/账户/客户/金额/摘要。"""

    class Meta:
        model = Receipt
        fields = ["doc_date", "bank_account", "customer", "amount", "summary"]
        widgets = {"doc_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["bank_account"].queryset = BankAccount.objects.filter(
                company=company, is_active=True)
            self.fields["customer"].queryset = Customer.objects.filter(
                company=company, is_active=True)
        self.fields["customer"].required = False
        self.fields["customer"].empty_label = "（其他收款，可不选）"
        for name, field in self.fields.items():
            w = field.widget
            w.attrs.setdefault("class", "form-select" if isinstance(w, forms.Select) else "form-control")

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("收款金额必须大于 0")
        return amount


# --- 票据登记（M3）-----------------------------------------------------------
class _NoteFormMixin:
    """票据表单公共：日期控件 ISO、字段样式、对手按账套过滤。"""

    def _style(self):
        for name, field in self.fields.items():
            w = field.widget
            if isinstance(w, forms.DateInput):
                w.attrs.setdefault("type", "date")
                w.format = "%Y-%m-%d"
            w.attrs.setdefault("class", "form-select" if isinstance(w, forms.Select) else "form-control")


class NoteReceivableForm(_NoteFormMixin, forms.ModelForm):
    class Meta:
        model = NoteReceivable
        fields = ["note_no", "draw_date", "due_date", "customer", "amount", "remark"]
        widgets = {
            "draw_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "due_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["customer"].queryset = Customer.objects.filter(company=company, is_active=True)
        self._style()

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("票面金额必须大于 0")
        return amount


class NotePayableForm(_NoteFormMixin, forms.ModelForm):
    class Meta:
        model = NotePayable
        fields = ["note_no", "draw_date", "due_date", "supplier", "amount", "remark"]
        widgets = {
            "draw_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "due_date": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }

    def __init__(self, *args, company=None, **kwargs):
        super().__init__(*args, **kwargs)
        if company is not None:
            self.fields["supplier"].queryset = Supplier.objects.filter(company=company, is_active=True)
        self._style()

    def clean_amount(self):
        amount = self.cleaned_data["amount"]
        if amount is None or amount <= 0:
            raise forms.ValidationError("票面金额必须大于 0")
        return amount
