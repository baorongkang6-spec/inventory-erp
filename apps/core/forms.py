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
