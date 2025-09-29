from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils import timezone
from .models import Appointment
import json

@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    # List display configuration
    list_display = [
        'id',
        'customer_name_display',
        'phone_number_display',
        'service_display',
        'area_display',
        'status_badge',
        'scheduled_datetime_display',
        'created_at_display',
        'conversation_count',
        'actions_column'
    ]
    
    # Filters
    list_filter = [
        'status',
        'project_type',
        'property_type',
        'house_stage',
        'has_plan',
        ('created_at', admin.DateFieldListFilter),
        ('scheduled_datetime', admin.DateFieldListFilter),
        ('updated_at', admin.DateFieldListFilter)
    ]
    
    # Search functionality
    search_fields = [
        'customer_name',
        'phone_number',
        'customer_area',
        'timeline',
        'project_description'  # Changed from 'notes'
    ]
    
    # Default ordering
    ordering = ['-created_at']
    
    # Items per page
    list_per_page = 25
    
    # Fields to display in detail view
    fieldsets = (
        ('Customer Information', {
            'fields': ('customer_name', 'phone_number', 'customer_area'),
            'classes': ('wide',)
        }),
        ('Project Details', {
            'fields': ('project_type', 'has_plan', 'property_type', 'house_stage', 'timeline'),
            'classes': ('wide',)
        }),
        ('Appointment', {
            'fields': ('status', 'scheduled_datetime', 'project_description'),  # Changed from 'notes'
            'classes': ('wide',)
        }),
        ('System Information', {
            'fields': ('created_at', 'updated_at'),  # Removed 'retry_count'
            'classes': ('collapse',)
        }),
        ('Conversation History', {
            'fields': ('conversation_display',),
            'classes': ('collapse',)
        })
    )
    
    # Read-only fields
    readonly_fields = [
        'created_at', 
        'updated_at', 
        'conversation_display',
        'conversation_count'
    ]   
    # Custom display methods
    def customer_name_display(self, obj):
        """Display customer name with fallback"""
        if obj.customer_name:
            return obj.customer_name
        return format_html('<em style="color: #666;">No name yet</em>')
    customer_name_display.short_description = 'Customer Name'
    customer_name_display.admin_order_field = 'customer_name'
    
    def phone_number_display(self, obj):
        """Display phone number with WhatsApp link"""
        if obj.phone_number:
            clean_number = obj.phone_number.replace('whatsapp:', '')
            return format_html(
                '<a href="https://wa.me/{}" target="_blank" style="color: #25D366;">📱 {}</a>',
                clean_number.replace('+', ''),
                clean_number
            )
        return '-'
    phone_number_display.short_description = 'Phone Number'
    phone_number_display.admin_order_field = 'phone_number'
    
    def service_display(self, obj):
        """Display service type with icon"""
        if obj.project_type:
            icons = {
                'bathroom renovation': '🛁',
                'kitchen renovation': '🏠',
                'new plumbing installation': '🔧'
            }
            icon = icons.get(obj.project_type, '🔧')
            
            # Use direct field access if get_project_type_display doesn't exist
            display_text = obj.project_type.replace('_', ' ').title()
            if hasattr(obj, 'get_project_type_display'):
                display_text = obj.get_project_type_display()
                
            return format_html('{} {}', icon, display_text)
        return format_html('<em style="color: #666;">Not specified</em>')
    service_display.short_description = 'Service'
    service_display.admin_order_field = 'project_type'
    
    def area_display(self, obj):
        """Display customer area"""
        if obj.customer_area:
            return format_html('📍 {}', obj.customer_area)
        return format_html('<em style="color: #666;">No area</em>')
    area_display.short_description = 'Area'
    area_display.admin_order_field = 'customer_area'
    
    def status_badge(self, obj):
        """Display status with colored badge"""
        colors = {
            'pending': '#ffc107',    # Yellow
            'in_progress': '#17a2b8', # Blue
            'confirmed': '#28a745',   # Green
            'completed': '#6c757d',   # Gray
            'cancelled': '#dc3545'    # Red
        }
        color = colors.get(obj.status, '#6c757d')
        
        # Use direct field access if get_status_display doesn't exist
        status_text = obj.status.replace('_', ' ').title()
        if hasattr(obj, 'get_status_display'):
            status_text = obj.get_status_display()
            
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-size: 11px; font-weight: bold;">{}</span>',
            color,
            status_text.upper()
        )
    status_badge.short_description = 'Status'
    status_badge.admin_order_field = 'status'
    
    def scheduled_datetime_display(self, obj):
        """Display scheduled datetime with relative time"""
        if obj.scheduled_datetime:
            now = timezone.now()
            if obj.scheduled_datetime > now:
                # Future appointment
                return format_html(
                    '<strong style="color: #28a745;">📅 {}</strong><br><small>In {}</small>',
                    obj.scheduled_datetime.strftime('%b %d, %Y at %I:%M %p'),
                    self.time_until(obj.scheduled_datetime)
                )
            else:
                # Past appointment
                return format_html(
                    '<span style="color: #dc3545;">📅 {}</span><br><small>{} ago</small>',
                    obj.scheduled_datetime.strftime('%b %d, %Y at %I:%M %p'),
                    self.time_since(obj.scheduled_datetime)
                )
        return format_html('<em style="color: #666;">Not scheduled</em>')
    scheduled_datetime_display.short_description = 'Scheduled'
    scheduled_datetime_display.admin_order_field = 'scheduled_datetime'
    
    def created_at_display(self, obj):
        """Display creation time with relative time"""
        return format_html(
            '{}<br><small style="color: #666;">{} ago</small>',
            obj.created_at.strftime('%b %d, %Y'),
            self.time_since(obj.created_at)
        )
    created_at_display.short_description = 'Created'
    created_at_display.admin_order_field = 'created_at'
    
    def completion_percentage(self, obj):
        """Show completion percentage with progress bar"""
        percentage = 0
        if hasattr(obj, 'get_customer_info_completeness'):
            percentage = obj.get_customer_info_completeness()
        color = '#28a745' if percentage >= 80 else '#ffc107' if percentage >= 50 else '#dc3545'
        return format_html(
            '<div style="background-color: #e9ecef; border-radius: 10px; overflow: hidden; width: 100px; height: 20px;">'
            '<div style="background-color: {}; height: 100%; width: {}%; display: flex; align-items: center; justify-content: center; color: white; font-size: 11px; font-weight: bold;">'
            '{}%</div></div>',
            color, percentage, int(percentage)
        )
    completion_percentage.short_description = 'Complete'
    
    def conversation_count(self, obj):
        """Display conversation message count"""
        try:
            if obj.conversation_history:
                if isinstance(obj.conversation_history, str):
                    conversation = json.loads(obj.conversation_history)
                    count = len(conversation)
                else:
                    count = len(obj.conversation_history)
            else:
                count = 0
            return format_html(
                '<span style="background-color: #007bff; color: white; padding: 2px 6px; border-radius: 10px; font-size: 11px;">💬 {}</span>',
                count
            )
        except:
            return format_html('<span style="color: #666;">0</span>')
    conversation_count.short_description = 'Messages'
    
    def actions_column(self, obj):
        """Quick action buttons"""
        actions = []
        
        # WhatsApp link
        if obj.phone_number:
            clean_number = obj.phone_number.replace('whatsapp:', '').replace('+', '')
            actions.append(
                format_html(
                    '<a href="https://wa.me/{}" target="_blank" style="color: #25D366; text-decoration: none;" title="Message on WhatsApp">💬</a>',
                    clean_number
                )
            )
        
        # View details link
        actions.append(
            format_html(
                '<a href="{}" style="color: #007bff; text-decoration: none;" title="View details">👁️</a>',
                reverse('admin:{}_{}_change'.format(obj._meta.app_label, obj._meta.model_name), args=[obj.pk])
            )
        )
        
        # Mark as completed if confirmed
        if obj.status == 'confirmed':
            actions.append(
                format_html(
                    '<a href="javascript:void(0)" onclick="markCompleted({})" style="color: #28a745; text-decoration: none;" title="Mark as completed">✅</a>',
                    obj.pk
                )
            )
        
        return format_html(' | '.join(actions))
    actions_column.short_description = 'Actions'
    
    def conversation_display(self, obj):
        """Display conversation history in a readable format"""
        if not obj.conversation_history:
            return format_html('<em>No conversation history</em>')
        
        try:
            # Handle case where conversation_history might be a string
            if isinstance(obj.conversation_history, str):
                conversation_data = json.loads(obj.conversation_history)
            else:
                conversation_data = obj.conversation_history
            
            conversation_html = []
            for message in conversation_data:
                role = message.get('role', 'unknown')
                content = message.get('content', '')
                
                if role == 'user':
                    conversation_html.append(
                        format_html(
                            '<div style="margin-bottom: 10px; padding: 8px; background-color: #e3f2fd; border-left: 3px solid #2196f3; border-radius: 4px;">'
                            '<strong>Customer:</strong> {}</div>',
                            content
                        )
                    )
                elif role == 'assistant':
                    conversation_html.append(
                        format_html(
                            '<div style="margin-bottom: 10px; padding: 8px; background-color: #f3e5f5; border-left: 3px solid #9c27b0; border-radius: 4px;">'
                            '<strong>Sarah (Bot):</strong> {}</div>',
                            content
                        )
                    )
            
            return mark_safe(''.join(conversation_html))
        except Exception as e:
            return format_html('<em>Error displaying conversation: {}</em>', str(e))
    conversation_display.short_description = 'Conversation History'
    
    # Utility methods
    def time_since(self, datetime_obj):
        """Calculate time since given datetime"""
        now = timezone.now()
        diff = now - datetime_obj
        
        if diff.days > 0:
            return f"{diff.days} day{'s' if diff.days != 1 else ''}"
        elif diff.seconds > 3600:
            hours = diff.seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''}"
        elif diff.seconds > 60:
            minutes = diff.seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        else:
            return "Just now"
    
    def time_until(self, datetime_obj):
        """Calculate time until given datetime"""
        now = timezone.now()
        diff = datetime_obj - now
        
        if diff.days > 0:
            return f"{diff.days} day{'s' if diff.days != 1 else ''}"
        elif diff.seconds > 3600:
            hours = diff.seconds // 3600
            return f"{hours} hour{'s' if hours != 1 else ''}"
        elif diff.seconds > 60:
            minutes = diff.seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        else:
            return "Very soon"
    
    # Custom admin actions
    actions = ['mark_as_completed', 'mark_as_cancelled', 'export_as_csv']
    
    def mark_as_completed(self, request, queryset):
        """Mark selected appointments as completed"""
        updated = queryset.update(status='completed')
        self.message_user(request, f'{updated} appointment(s) marked as completed.')
    mark_as_completed.short_description = "Mark selected appointments as completed"
    
    def mark_as_cancelled(self, request, queryset):
        """Mark selected appointments as cancelled"""
        updated = queryset.update(status='cancelled')
        self.message_user(request, f'{updated} appointment(s) marked as cancelled.')
    mark_as_cancelled.short_description = "Mark selected appointments as cancelled"
    
    def export_as_csv(self, request, queryset):
        """Export selected appointments as CSV"""
        import csv
        from django.http import HttpResponse
        
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = 'attachment; filename="appointments.csv"'
        
        writer = csv.writer(response)
        writer.writerow([
            'ID', 'Customer Name', 'Phone Number', 'Area', 'Service Type',
            'Property Type', 'House Stage', 'Timeline', 'Has Plan',
            'Status', 'Scheduled DateTime', 'Created At', 'Notes'
        ])
        
        for appointment in queryset:
            # Handle project_type display
            project_type = appointment.project_type
            if hasattr(appointment, 'get_project_type_display'):
                project_type = appointment.get_project_type_display()
            elif project_type:
                project_type = project_type.replace('_', ' ').title()
            
            # Handle property_type display
            property_type = appointment.property_type
            if hasattr(appointment, 'get_property_type_display'):
                property_type = appointment.get_property_type_display()
            elif property_type:
                property_type = property_type.replace('_', ' ').title()
            
            # Handle house_stage display
            house_stage = appointment.house_stage
            if hasattr(appointment, 'get_house_stage_display'):
                house_stage = appointment.get_house_stage_display()
            elif house_stage:
                house_stage = house_stage.replace('_', ' ').title()
            
            # Handle status display
            status = appointment.status
            if hasattr(appointment, 'get_status_display'):
                status = appointment.get_status_display()
            elif status:
                status = status.replace('_', ' ').title()
            
            writer.writerow([
                appointment.id,
                appointment.customer_name or '',
                appointment.phone_number or '',
                appointment.customer_area or '',
                project_type or '',
                property_type or '',
                house_stage or '',
                appointment.timeline or '',
                'Yes' if appointment.has_plan else 'No' if appointment.has_plan is False else 'Unknown',
                status,
                appointment.scheduled_datetime.strftime('%Y-%m-%d %H:%M') if appointment.scheduled_datetime else '',
                appointment.created_at.strftime('%Y-%m-%d %H:%M'),
                appointment.notes or ''
            ])
        
        return response
    export_as_csv.short_description = "Export selected appointments as CSV"
    
    # Custom admin views
    def changelist_view(self, request, extra_context=None):
        """Add custom context to changelist view"""
        extra_context = extra_context or {}
        
        # Add statistics
        total_appointments = Appointment.objects.count()
        pending_appointments = Appointment.objects.filter(status='pending').count()
        confirmed_appointments = Appointment.objects.filter(status='confirmed').count()
        completed_appointments = Appointment.objects.filter(status='completed').count()
        
        today = timezone.now().date()
        today_appointments = Appointment.objects.filter(
            scheduled_datetime__date=today
        ).count()
        
        extra_context.update({
            'total_appointments': total_appointments,
            'pending_appointments': pending_appointments,
            'confirmed_appointments': confirmed_appointments,
            'completed_appointments': completed_appointments,
            'today_appointments': today_appointments,
        })
        
        return super().changelist_view(request, extra_context=extra_context)
    
    # Add custom CSS and JavaScript
    class Media:
        css = {
            'all': ('admin/custom_admin.css',)
        }
        js = ('admin/custom_admin.js',)