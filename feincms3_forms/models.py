import contextlib
import re
import warnings
from functools import partial, reduce

from content_editor.models import Type
from django import forms
from django.core import validators
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.db import models
from django.db.models import F, Value, signals
from django.db.models.fields import BLANK_CHOICE_DASH
from django.template.defaultfilters import truncatechars
from django.utils.crypto import get_random_string
from django.utils.functional import cached_property
from django.utils.module_loading import import_string
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _
from feincms3.utils import ChoicesCharField, validation_error
from django_select2 import forms as s2forms
from django_celery_beat.models import CrontabSchedule


class FormType(Type):
    _REQUIRED = {"key", "label", "regions", "form_class", "validate"}

    def __init__(self, **kwargs):
        kwargs.setdefault("form_class", forms.Form)
        kwargs.setdefault("validate", lambda configured_form: [])
        super().__init__(**kwargs)

    def __getattr__(self, attr):
        value = super().__getattr__(attr)
        if isinstance(value, str) and re.match(r"^\w+\.([\w\.]+)+$", value):
            with contextlib.suppress(ModuleNotFoundError):
                value = import_string(value)

        setattr(self, attr, value)
        return value


RANDOM_STRING_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"


class NameField(models.CharField):
    """Almost but not quite a slug field. We only allow snake_case_names."""

    def __init__(self, **kwargs):
        kwargs.setdefault("verbose_name", _("name"))
        kwargs.setdefault("max_length", 50)
        kwargs.setdefault(
            "validators",
            [
                validators.RegexValidator(
                    r"^[a-z0-9_]+$",
                    message=_(
                        "Enter a value consisting only of lowercase letters,"
                        " numbers and the underscore."
                    ),
                ),
            ],
        )
        kwargs.setdefault(
            "help_text",
            _(
                "Data is saved using this name. Changing it may result in data loss."
                " This field only allows a-z, 0-9 and _ as characters."
            ),
        )
        super().__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        return name, "django.db.models.CharField", args, kwargs

    def formfield(self, **kwargs):
        kwargs.setdefault("required", False)
        return super().formfield(**kwargs)

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return f"field_{get_random_string(10, allowed_chars=RANDOM_STRING_CHARS)}"
        return value


class FormFieldBase(models.Model):
    """
    Form field plugins must inherit this model
    """

    name = NameField()

    class Meta:
        abstract = True

    def get_fields(self, **kwargs):
        """
        Return a dictionary of form fields

        The keys are form field names (prefixed by ``self.name``), the values
        form field instances.
        """
        raise NotImplementedError(
            f"{self._meta.label_lower} needs a get_fields implementation"
        )

    def get_initial(self):
        """The default implementation returns no initial values"""
        return {}

    def get_cleaners(self):
        """
        Return a list of ``clean()`` hooks which receive the form instance,
        return the cleaned data and may optionally raise ``ValidationError``
        instances.
        """
        return []

    def get_loaders(self):
        """
        Return a list of loaders

        Loaders are callables which receive the serialized form data and
        return a dictionary of the following shape::

            {"name": ..., "label": ..., "value": ...}

        """
        raise NotImplementedError(
            f"{self._meta.label_lower} needs a get_loaders implementation"
        )

    @staticmethod
    def set_field_type(sender, **kwargs):
        if issubclass(sender, FormFieldBase) and not sender._meta.abstract:
            sender.type = sender.__name__.lower()


signals.class_prepared.connect(FormFieldBase.set_field_type)


class ConfiguredForm(models.Model):
    name = models.CharField(_("name"), max_length=1000)
    form_type = ChoicesCharField(_("form type"), max_length=100)

    class Meta:
        abstract = True
        ordering = ["name"]
        verbose_name = _("configured form")
        verbose_name_plural = _("configured form")

    def __str__(self):
        return self.name

    @cached_property
    def regions(self):
        try:
            regions = self.type.regions
        except (AttributeError, KeyError):
            return []
        return regions(self) if callable(regions) else regions

    @staticmethod
    def fill_form_choices(sender, **kwargs):
        if issubclass(sender, ConfiguredForm) and not sender._meta.abstract:
            field = sender._meta.get_field("form_type")
            field.choices = [(row["key"], row["label"]) for row in sender.FORMS]

            types = {type.key: type for type in sender.FORMS}
            sender.type = property(lambda self: types.get(self.form_type))

    def get_formfields_union(self, *, plugins, attributes=None):

        # Поля по умолчанию для атрибутов, которых нет в модели плагина
        FIELD_DEFAULTS = {
            "is_collapsible": (False, models.BooleanField()),
            "is_collapse_below": (False, models.BooleanField()),
            "is_collapsed_by_default": (False, models.BooleanField()),
        }

        values = ["name"]
        columns = []
        for index, attribute in enumerate(attributes or []):
            alias = f"__val_{index}"
            values.append(alias)
            columns.append((alias, attribute))

        querysets = []
        for plugin in plugins:
            if not issubclass(plugin, FormFieldBase):
                continue
            qs = plugin.objects.filter(parent=self)
            annotations = {}
            for alias, attribute in columns:
                # See https://code.djangoproject.com/ticket/28553
                # If we could rely on values_list returning columns in the
                # specified order **for all querysets** we wouldn't have to do
                # this. But since that isn't the case we use .annotate() for
                # all values, even those which 1:1 exist as a column in the
                # database. I'm not sure if the enumeration is necessary but it
                # certainly doesn't hurt (more).
                try:
                    plugin._meta.get_field(attribute)
                except FieldDoesNotExist:
                    # сначала пробуем взять значение из Python-атрибута
                    value = getattr(plugin, attribute, None)
                    if value is not None:
                        annotations[alias] = Value(value, output_field=models.TextField())
                    else:
                        # Если поля нет в модели плагина и свойствах, используем значение по умолчанию из FIELD_DEFAULTS
                        default, field_type = FIELD_DEFAULTS.get(attribute, ("", models.TextField()))
                        annotations[alias] = Value(default, output_field=field_type)
                else:
                    annotations[alias] = F(attribute)
            qs = qs.annotate(**annotations)
            querysets.append(qs.values_list(*values))
        qs = reduce(lambda p, q: p.union(q, all=True), querysets[1:], querysets[0])
        return [
            (row[0], {column[1]: value for column, value in zip(columns, row[1:])})
            for row in qs
        ]


signals.class_prepared.connect(ConfiguredForm.fill_form_choices)


class FormField(FormFieldBase):
    label = models.CharField(_("label"), max_length=1000)
    is_required = models.BooleanField(_("is required"), default=True)
    help_text = models.CharField(
        _("help text"),
        max_length=1000,
        blank=True,
    )

    # CUSTOM FIELDS

    # is_unique = models.BooleanField(
    #     default=False, verbose_name=_("Поле должно быть уникальным")
    # )

    is_table_visible = models.BooleanField(
            _("Виден в табличном режиме"), default=True
        )
    is_modal_visible = models.BooleanField(
            _("Виден в модальном режиме"), default=True
        )
    is_card_visible = models.BooleanField(
        _("Виден в карточном режиме"), default=False
    )
    is_modal_detail_visible = models.BooleanField(
        _("Виден в модальном окне детализации"), default=True
    )

    is_visible = models.BooleanField(
        _("Отображать поле в форме"), default=True
    )  # custom Field, если is_visible == False,

    is_editable = models.BooleanField(
        _("Позволять редактировать это поле"), default=True
    )

    is_required_to_move = models.BooleanField(
        _("Обязательно для перехода"),
        default=False,
        help_text=_(
            "Указывает, обязательно ли заполнение этого поля для перехода на следующий этап."
        ),
    )  # Custom Field

    # Использование скрипта
    use_script = models.BooleanField(
        default=False, verbose_name=_("Использовать скрипт")
    )
    execute_on_first_save = models.BooleanField(
        default=False, verbose_name=_("Выполнить скрипт в момент создания записи")
    )
    execute_on_every_save = models.BooleanField(
        default=False, verbose_name=_("Выполнять скрипт при каждом сохранении")
    )

    # Перенесено в модель формы, чтобы не дублировать
    # execute_periodically = models.BooleanField(
    #     default=False,
    #     verbose_name=_("Выполнять периодически"),
    #     help_text=_("Выполнять периодически пока поле не будет заполнено."),
    # )
    # crontab = models.ForeignKey(
    #     CrontabSchedule,
    #     on_delete=models.PROTECT,
    #     null=True,
    #     blank=True,
    #     verbose_name=_("Периодичность"),
    # )
    python_script = models.TextField(
        null=True,
        blank=True,
        verbose_name=_("Python-скрипт"),
        help_text="Доступен атрибут self записи",
    )
    validation_python_script = models.TextField(
        null=True,
        blank=True,
        verbose_name=_("Python-скрипт валидации"),
        help_text="Доступен атрибут self записи, обрабатывает ValidationError",
    )

    class Meta:
        abstract = True

    def __str__(self):
        return truncatechars(self.label, 50)

    def should_show_field(self, is_update=False):
        """
        Определяет, должно ли поле отображаться (в формах).

        Условия:
          - Если is_visible == False, поле не показывается.
          - Если is_visible == True и is_editable == True, поле всегда показывается.
          - Если is_visible == True и is_editable == False, поле показывается только в режиме создания (is_update == False).
        """
        if not self.is_visible:
            return False
        elif self.is_editable:
            return True
        else:
            return not is_update

    def get_field(self, *, form_class, should_show, **kwargs):

        # Если поле не должно показываться, оно становится is_required=False, иначе self.is_required
        is_required = self.is_required if should_show else False

        kwargs.setdefault("label", self.label)
        kwargs.setdefault("required", is_required)
        kwargs.setdefault("help_text", self.help_text)
        return {self.name: form_class(**kwargs)}

    def get_fields(self, **kwargs):
        warnings.warn(
            "Replace super().get_fields() with self.get_field() now.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.get_field(**kwargs)

    def get_loaders(self):
        return [partial(simple_loader, label=self.label, name=self.name)]


def simple_loader(data, *, name, label):
    return {"name": name, "label": label, "value": data.get(name)}


class SimpleFieldBase(FormField):
    class Type(models.TextChoices):
        TEXT = "text", _("text field")
        EMAIL = "email", _("email address field")
        URL = "url", _("URL field")
        DATE = "date", _("date field")
        INTEGER = "integer", _("integer field")
        TEXTAREA = "textarea", _("multiline text field")
        CHECKBOX = "checkbox", _("checkbox field")
        SELECT = "select", _("dropdown field")
        RADIO = "radio", _("radio input field")
        SELECT_MULTIPLE = "select-multiple", _("select multiple")
        CHECKBOX_SELECT_MULTIPLE = "checkbox-select-multiple", _("multiple checkboxes")

    type = models.CharField(_("type"), max_length=1000, editable=False)

    choices = models.TextField(
        _("choices"),
        help_text=_(
            "Enter one choice per line. You may optionally provide the"
            " value and the label separated by a pipe symbol (|)."
        ),
    )
    placeholder = models.CharField(
        _("placeholder"),
        max_length=1000,
        blank=True,
    )
    default_value = models.CharField(
        _("default value"),
        max_length=1000,
        blank=True,
        help_text=_("Optional default value of the field."),
    )
    max_length = models.PositiveIntegerField(_("max length"), blank=True, null=True)

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if hasattr(self, "TYPE"):
            self.type = self.TYPE
        super().save(*args, **kwargs)

    save.alters_data = True

    @classmethod
    def proxy(cls, type_name, **meta):
        meta["proxy"] = True
        meta["app_label"] = cls._meta.app_label

        if "verbose_name" not in meta and hasattr(type_name, "label"):
            meta["verbose_name"] = type_name.label

        meta_class = type("Meta", (cls.Meta,), meta)

        return type(
            f"{cls.__qualname__}_{type_name}",
            (cls,),
            {
                "__module__": cls.__module__,
                "Meta": meta_class,
                "TYPE": type_name,
            },
        )

    def clean_fields(self, exclude=None):
        super().clean_fields(exclude)

        if (
            self.choices
            and self.default_value
            # and slugify(self.default_value) not in dict(self.get_choices())  # Original
            and self.default_value not in dict(self.get_choices())
        ):
            raise validation_error(
                _(
                    'The specified default value "%(default)s" isn\'t part of the available choices.'
                )
                % {"default": self.default_value},
                field="default_value",
                exclude=exclude,
            )

    def get_choices(self):
        def _choice(value):
            parts = [part.strip() for part in value.split("|", 1)]
            if len(parts) == 1:
                # return (slugify(value), value)  # Original
                return (value, value)
            else:
                return tuple(parts)

        return [_choice(value) for value in self.choices.splitlines() if value]

    def get_initial(self):
        if not self.default_value:
            return {}
        if self.choices:
            # return {self.name: slugify(self.default_value)}  # Original
            return {self.name: self.default_value}
        return {self.name: self.default_value}

    def get_fields(self, **kwargs):

        # Custom Flags 18/02/2025
        is_update = kwargs.pop("is_update", False)
        # Определяем, показывать ли поле
        should_show = self.should_show_field(is_update=is_update)

        type = self.Type

        if self.type == type.TEXT:
            return self.get_field(
                form_class=forms.CharField,
                should_show=should_show,
                max_length=self.max_length,
                widget=(
                    forms.CharField.widget(
                        attrs={"placeholder": self.placeholder or False}
                    )
                    if should_show
                    else forms.HiddenInput
                ),
            )

        elif self.type == type.EMAIL:
            return self.get_field(
                form_class=forms.EmailField,
                should_show=should_show,
                widget=(
                    forms.EmailField.widget(
                        attrs={"placeholder": self.placeholder or False}
                    )
                    if should_show
                    else forms.HiddenInput
                ),
            )

        elif self.type == type.URL:
            return self.get_field(
                form_class=forms.URLField,
                should_show=should_show,
                widget=(
                    forms.URLField.widget(
                        attrs={"placeholder": self.placeholder or False}
                    )
                    if should_show
                    else forms.HiddenInput
                ),
            )

        elif self.type == type.DATE:
            return self.get_field(
                form_class=forms.DateField,
                should_show=should_show,
                widget=(
                    forms.DateInput(
                        attrs={"placeholder": self.placeholder or False, "type": "date"}
                    )
                    if should_show
                    else forms.HiddenInput
                ),
            )

        elif self.type == type.INTEGER:
            return self.get_field(
                form_class=forms.IntegerField,
                should_show=should_show,
                widget=(
                    forms.IntegerField.widget(
                        attrs={"placeholder": self.placeholder or False}
                    )
                    if should_show
                    else forms.HiddenInput
                ),
            )

        elif self.type == type.TEXTAREA:
            return self.get_field(
                form_class=forms.CharField,
                should_show=should_show,
                max_length=self.max_length,
                widget=(
                    forms.Textarea(
                        attrs={
                            "maxlength": self.max_length or False,
                            "placeholder": self.placeholder or False,
                            "rows": 5,
                        },
                    )
                    if should_show
                    else forms.HiddenInput
                ),
            )

        elif self.type == type.CHECKBOX:
            return self.get_field(
                form_class=forms.BooleanField,
                should_show=should_show,
                widget=forms.CheckboxInput if should_show else forms.HiddenInput,
            )

        elif self.type == type.SELECT:
            choices = self.get_choices()
            if not self.is_required or not self.default_value:
                blank_choice = (
                    [("", self.placeholder)] if self.placeholder else BLANK_CHOICE_DASH
                )
                choices = blank_choice + choices
            return self.get_field(
                form_class=forms.ChoiceField,
                should_show=should_show,
                choices=choices,
                # 30/10/2024 - инициализация select2 widget библиотеки django_select2
                widget=(
                    s2forms.Select2Widget(
                        attrs={
                            "data-minimum-input-length": 0,  # Количество символов, чтобы начать поиск
                            "data-maximum-input-length": 1000,  # Максимальное кол-во символов в input
                            "data-placeholder": "Нажмите для выбора",  # Надпись
                            "data-close-on-select": "true",  # Закрывать селектор после выбора
                            "data-allow-clear": "true",  # Иконка закрыть
                            "data-language": "ru",
                        }
                    )
                    if should_show
                    else forms.HiddenInput
                ),
            )

        elif self.type == type.RADIO:
            return self.get_field(
                form_class=forms.ChoiceField,
                should_show=should_show,
                widget=forms.RadioSelect if should_show else forms.HiddenInput,
                choices=self.get_choices(),
            )

        elif self.type == type.SELECT_MULTIPLE:
            return self.get_field(
                form_class=forms.MultipleChoiceField,
                should_show=should_show,
                choices=self.get_choices(),
                widget=(
                    forms.SelectMultiple if should_show else forms.MultipleHiddenInput
                ),
            )

        elif self.type == type.CHECKBOX_SELECT_MULTIPLE:
            return self.get_field(
                form_class=forms.MultipleChoiceField,
                should_show=should_show,
                widget=(
                    forms.CheckboxSelectMultiple
                    if should_show
                    else forms.MultipleHiddenInput
                ),
                choices=self.get_choices(),
            )

        else:
            raise ImproperlyConfigured(
                f"Model {self!r} has unhandled type {self.type!r}"
            )
