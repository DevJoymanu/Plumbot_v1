from django import forms
from .models import Appointment, Quotation, QuotationItem, QuotationTemplate, QuotationTemplateItem
import json
from django.forms import inlineformset_factory

# Add to your bot/forms.py (create the file if it doesn't exist)


class AppointmentForm(forms.ModelForm):
    scheduled_datetime = forms.DateTimeField(
        widget=forms.DateTimeInput(attrs={'type': 'datetime-local'}),
        required=False
    )
    
    class Meta:
        model = Appointment
        fields = [
            'customer_name', 'phone_number', 'project_type',
            'property_type', 'customer_area', 'timeline',
            'scheduled_datetime', 'status', 'has_plan'
        ]

class SettingsForm(forms.Form):
    # WhatsApp credentials are per-tenant and live on TenantWhatsAppChannel
    # (Meta Cloud API), not in this global form — the Twilio SID/token/number
    # fields were removed with Twilio.
    team_numbers = forms.CharField(
        label='Team Notification Numbers',
        widget=forms.Textarea,
        required=False,
        help_text='One number per line, format: whatsapp:+263774819901'
    )

class CalendarSettingsForm(forms.Form):
    google_calendar_credentials = forms.CharField(
        label='Google Calendar Credentials (JSON)',
        widget=forms.Textarea,
        required=False
    )
    calendar_id = forms.CharField(
        label='Calendar ID',
        max_length=100,
        required=False,
        initial='primary'
    )

class AISettingsForm(forms.Form):
    deepseek_api_key = forms.CharField(
        label='DeepSeek API Key',
        max_length=100,
        required=True,
        widget=forms.PasswordInput()
    )
    ai_temperature = forms.FloatField(
        label='AI Temperature (0-1)',
        min_value=0,
        max_value=1,
        required=True,
        initial=0.7
    )



class QuotationForm(forms.ModelForm):
    class Meta:
        model = Quotation
        fields = ['appointment', 'labor_cost', 'materials_cost', 'transport_cost', 'notes']
        widgets = {
            'appointment': forms.HiddenInput(),  # Use hidden input if appointment is set automatically
            'labor_cost': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'materials_cost': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'transport_cost': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
            'notes': forms.Textarea(attrs={'rows': 4, 'placeholder': 'Additional notes for the customer...'}),
        }
        labels = {
            'labor_cost': 'Labor Cost (R)',
            'materials_cost': 'Materials Cost (R)',
        }
class QuotationItemForm(forms.ModelForm):
    class Meta:
        model = QuotationItem
        fields = ['description', 'quantity', 'unit_price']
        widgets = {
            'description': forms.Textarea(attrs={'rows': 2, 'placeholder': 'Item description...'}),
            'quantity': forms.NumberInput(attrs={'step': '0.5', 'min': '0.5'}),
            'unit_price': forms.NumberInput(attrs={'step': '0.01', 'min': '0'}),
        }

# Create formset factory
QuotationItemFormSet = forms.inlineformset_factory(
    Quotation, 
    QuotationItem, 
    form=QuotationItemForm,
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True
)    

class QuotationTemplateForm(forms.ModelForm):
    class Meta:
        model = QuotationTemplate
        fields = [
            'name', 
            'description', 
            'project_type',
            'default_labor_cost',
            'default_transport_cost',
            'is_active'
        ]
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'e.g., Standard Bathroom Renovation'
            }),
            'description': forms.Textarea(attrs={
                'class': 'form-control',
                'rows': 3,
                'placeholder': 'Describe what this template includes...'
            }),
            'project_type': forms.Select(attrs={'class': 'form-control'}),
            # pbq-total-input is the quote editor's compact, right-aligned
            # figure box: these two sit in a .pbq-total-row on the builder, the
            # same row the quote editor puts labour and transport in, and a
            # full-width .form-control there looked like a different screen.
            'default_labor_cost': forms.NumberInput(attrs={
                'class': 'form-control pbq-total-input',
                'placeholder': '0.00',
                'step': '0.01',
                'inputmode': 'decimal',
            }),
            'default_transport_cost': forms.NumberInput(attrs={
                'class': 'form-control pbq-total-input',
                'placeholder': '0.00',
                'step': '0.01',
                'inputmode': 'decimal',
            }),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class QuotationTemplateItemForm(forms.ModelForm):
    class Meta:
        model = QuotationTemplateItem
        fields = [
            'description',
            'section',
            'quantity',
            'quantity_text',
            'unit_price',
            'category',
            'is_optional',
            'notes',
            'sort_order'
        ]
        widgets = {
            # bq-cell/bq-desc are the sectioned sheet's in-cell look; they
            # are inert on the flat builder, which never loads that stylesheet.
            'description': forms.TextInput(attrs={
                'class': 'form-control bq-cell bq-desc',
                'placeholder': 'Item description'
            }),
            # The section a row belongs to is never typed into the row: the
            # sectioned builder writes it from the heading above, and the flat
            # builder does not render it at all. Hidden on both, so neither
            # screen grows a field the plumber has to keep in step by hand.
            'section': forms.HiddenInput(),
            'quantity': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01'
            }),
            # The trade's own wording ("19 length"). The sectioned builder
            # shows this as the QTY cell and derives the number beside it.
            'quantity_text': forms.TextInput(attrs={
                'class': 'form-control bq-cell bq-qty',
                'placeholder': '0',
            }),
            'unit_price': forms.NumberInput(attrs={
                'class': 'form-control bq-cell bq-unit',
                'step': '0.01',
                'inputmode': 'decimal',
                'placeholder': '0.00',
            }),
            'category': forms.Select(attrs={'class': 'form-control'}),
            'is_optional': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'notes': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Optional notes'
            }),
            'sort_order': forms.NumberInput(attrs={'class': 'form-control'}),
        }


# Create formset for template items
# ONE blank card, exactly as the quote editor opens: min_num renders it, and
# the builder adds the next the moment that one is filled in. `extra` was 5,
# which was cheap as table rows and is five screens of scaffolding as the item
# cards the builder now uses.
QuotationTemplateItemFormSet = inlineformset_factory(
    QuotationTemplate,
    QuotationTemplateItem,
    form=QuotationTemplateItemForm,
    extra=0,
    can_delete=True,
    min_num=1,
    validate_min=True
)