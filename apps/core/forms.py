"""通用表单工具：给字段套 Bootstrap 样式（同时支持 Form 与 ModelForm）。"""

from django import forms


def style_fields(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget.attrs.setdefault("class", "form-select")
        elif isinstance(widget, (forms.DateInput,)):
            widget.attrs.setdefault("class", "form-control")
            widget.attrs.setdefault("type", "date")
            # <input type=date> 要求 ISO 格式，覆盖 zh-hans 本地化格式
            widget.format = "%Y-%m-%d"
        else:
            widget.attrs.setdefault("class", "form-control")


class BootstrapForm(forms.Form):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_fields(self)


class BootstrapModelForm(forms.ModelForm):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        style_fields(self)


class CompanyScopedModelForm(BootstrapModelForm):
    """company 字段不在表单里（由视图注入），但「公司内唯一」约束需要它。

    Django 默认会把表单外的字段从唯一性校验中排除，导致 (company, xxx) 约束被
    跳过、最终以 IntegrityError 500 报错。这里把 company 移出排除集，使重复在表单层
    就报友好错误。前提：视图已在校验前给 instance.company 赋值（见 CompanyScopedFormMixin）。
    """

    def _get_validation_exclusions(self):
        exclude = super()._get_validation_exclusions()
        exclude.discard("company")
        return exclude
