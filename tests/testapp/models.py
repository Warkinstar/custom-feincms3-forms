from content_editor.models import Region, create_plugin_base
from django import forms
from django.db import models
from django.utils.translation import gettext_lazy as _

from feincms3_forms import models as forms_models


class ConfiguredForm(forms_models.ConfiguredForm):
    FORMS = [
        forms_models.FormType(
            key="contact",
            label=_("contact form"),
            regions=[Region(key="form", title=_("form"))],
            validate="testapp.forms.validate_contact_form",
            process="testapp.forms.process_contact_form",
        ),
        forms_models.FormType(
            key="other-fields",
            label=_("other fields"),
            regions=[],
            form_class="testapp.forms.OtherFieldsForm",
        ),
    ]


ConfiguredFormPlugin = create_plugin_base(ConfiguredForm)


class PlainText(ConfiguredFormPlugin):
    text = models.TextField(_("text"))

    class Meta:
        verbose_name = _("text")

    def __str__(self):
        return self.text[:40]


class SimpleField(forms_models.SimpleFieldBase, ConfiguredFormPlugin):
    pass


Text = SimpleField.proxy(SimpleField.Type.TEXT)
Email = SimpleField.proxy(SimpleField.Type.EMAIL)
URL = SimpleField.proxy(SimpleField.Type.URL)
Date = SimpleField.proxy(SimpleField.Type.DATE)
Integer = SimpleField.proxy(SimpleField.Type.INTEGER)
Textarea = SimpleField.proxy(SimpleField.Type.TEXTAREA)
Checkbox = SimpleField.proxy(SimpleField.Type.CHECKBOX)
Select = SimpleField.proxy(SimpleField.Type.SELECT)
Radio = SimpleField.proxy(SimpleField.Type.RADIO, verbose_name="Listen to the radio")


class CaptchaField(ConfiguredFormPlugin):
    class Meta:
        abstract = True


class Duration(ConfiguredFormPlugin):
    label_from = models.CharField(_("from label"), max_length=1000)
    label_until = models.CharField(_("until label"), max_length=1000)
    name = forms_models.NameField()

    class Meta:
        verbose_name = _("duration")

    def __str__(self):
        return f"{self.label_from} - {self.label_until}"

    def get_fields(self, **kwargs):
        return {
            f"{self.name}_from": forms.DateField(
                label=self.label_from,
                required=True,
                widget=forms.DateInput(attrs={"type": "date"}),
            ),
            f"{self.name}_until": forms.DateField(
                label=self.label_until,
                required=True,
                widget=forms.DateInput(attrs={"type": "date"}),
            ),
        }


class HoneypotField(forms.CharField):
    widget = forms.HiddenInput

    def validate(self, value):
        super().validate(value)
        if value:
            raise forms.ValidationError(f"Invalid honeypot value {repr(value)}")


class Honeypot(ConfiguredFormPlugin):
    def get_fields(self, **kwargs):
        return {"honeypot": HoneypotField()}
