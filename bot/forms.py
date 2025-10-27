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
    twilio_account_sid = forms.CharField(
        label='Twilio Account SID',
        max_length=100,
        required=True
    )
    twilio_auth_token = forms.CharField(
        label='Twilio Auth Token',
        max_length=100,
        required=True,
        widget=forms.PasswordInput()
    )
    twilio_whatsapp_number = forms.CharField(
        label='Twilio WhatsApp Number',
        max_length=20,
        required=True
    )
    team_numbers = forms.CharField(
        label='Team Notification Numbers',
        widget=forms.Textarea,
        required=False,
        help_text='One number per line, format: whatsapp:+27610318200'
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
            'default_labor_cost': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': '0.00',
                'step': '0.01'
            }),
            'default_transport_cost': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': '0.00',
                'step': '0.01'
            }),
            'is_active': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
        }


class QuotationTemplateItemForm(forms.ModelForm):
    class Meta:
        model = QuotationTemplateItem
        fields = [
            'description',
            'quantity', 
            'unit_price',
            'category',
            'is_optional',
            'notes',
            'sort_order'
        ]
        widgets = {
            'description': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Item description'
            }),
            'quantity': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01'
            }),
            'unit_price': forms.NumberInput(attrs={
                'class': 'form-control',
                'step': '0.01'
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
QuotationTemplateItemFormSet = inlineformset_factory(
    QuotationTemplate,
    QuotationTemplateItem,
    form=QuotationTemplateItemForm,
    extra=5,
    can_delete=True,
    min_num=1,
    validate_min=True
)