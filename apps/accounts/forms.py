"""用户管理表单（M13）：新增/编辑用户、分配角色与可见公司。

角色 = Django Group（限 roles.ALL_ROLES 五个）；密码新建必填、编辑留空则不改。
"""

from django import forms
from django.contrib.auth.models import Group

from apps.accounts import roles
from apps.core.models import Company

from .models import User


class UserForm(forms.ModelForm):
    password = forms.CharField(
        label="密码", widget=forms.PasswordInput(render_value=False), required=False,
        help_text="新建用户必填；编辑时留空表示不修改密码。")
    roles = forms.ModelMultipleChoiceField(
        label="角色", queryset=Group.objects.none(),
        widget=forms.CheckboxSelectMultiple, required=False,
        help_text="一人可兼多角色，权限按角色叠加。")

    class Meta:
        model = User
        fields = ["username", "display_name", "is_active",
                  "can_view_all_companies", "companies"]
        widgets = {"companies": forms.CheckboxSelectMultiple}
        labels = {"is_active": "启用"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["roles"].queryset = Group.objects.filter(name__in=roles.ALL_ROLES)
        self.fields["companies"].queryset = Company.objects.filter(is_active=True)
        self.fields["companies"].required = False
        if self.instance and self.instance.pk:
            self.fields["roles"].initial = list(self.instance.groups.all())
            self.fields["username"].disabled = True  # 用户名建后不改
        for name, field in self.fields.items():
            w = field.widget
            if isinstance(w, forms.CheckboxSelectMultiple):
                w.attrs.setdefault("class", "form-check-input")
                continue
            if isinstance(w, forms.CheckboxInput):
                w.attrs.setdefault("class", "form-check-input")
                continue
            w.attrs.setdefault("class", "form-select" if isinstance(w, forms.Select) else "form-control")

    def clean(self):
        cd = super().clean()
        if not self.instance.pk and not cd.get("password"):
            self.add_error("password", "新建用户必须设置密码")
        return cd

    def save(self, commit=True):
        user = super().save(commit=False)
        pw = self.cleaned_data.get("password")
        if pw:
            user.set_password(pw)
        if commit:
            user.save()
            self.save_m2m()                       # companies
            user.groups.set(self.cleaned_data["roles"])
        return user
