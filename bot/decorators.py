from functools import wraps
from django.contrib.auth.decorators import login_required
from django.contrib.auth import authenticate, login
from django.shortcuts import redirect
from django.contrib import messages
from django.http import HttpResponseForbidden
from django.core.exceptions import PermissionDenied


def staff_required(view_func=None, *, redirect_url='login', message=None):
    """
    Decorator to ensure user is logged in and is a staff member.
    Can be used as @staff_required or @staff_required(redirect_url='custom_login')
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            # Check if user is authenticated
            if not request.user.is_authenticated:
                if message:
                    messages.warning(request, message)
                else:
                    messages.warning(request, 'Please log in to access this page.')
                return redirect(redirect_url)
            
            # Check if user is staff
            if not request.user.is_staff:
                if message:
                    messages.error(request, message)
                else:
                    messages.error(request, 'Staff access required.')
                raise PermissionDenied("Staff access required")
            
            return func(request, *args, **kwargs)
        return wrapper
    
    # Handle both @staff_required and @staff_required()
    if view_func is None:
        return decorator
    else:
        return decorator(view_func)


def superuser_required(view_func=None, *, redirect_url='login', message=None):
    """
    Decorator to ensure user is logged in and is a superuser.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            # Check if user is authenticated
            if not request.user.is_authenticated:
                if message:
                    messages.warning(request, message)
                else:
                    messages.warning(request, 'Please log in to access this page.')
                return redirect(redirect_url)
            
            # Check if user is superuser
            if not request.user.is_superuser:
                if message:
                    messages.error(request, message)
                else:
                    messages.error(request, 'Admin access required.')
                raise PermissionDenied("Admin access required")
            
            return func(request, *args, **kwargs)
        return wrapper
    
    if view_func is None:
        return decorator
    else:
        return decorator(view_func)


def anonymous_required(view_func=None, *, redirect_url='dashboard'):
    """
    Decorator to ensure user is NOT logged in.
    Useful for login/register pages.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            if request.user.is_authenticated:
                return redirect(redirect_url)
            return func(request, *args, **kwargs)
        return wrapper
    
    if view_func is None:
        return decorator
    else:
        return decorator(view_func)


def group_required(*group_names, redirect_url='login', message=None):
    """
    Decorator to check if user belongs to specific groups.
    Usage: @group_required('plumbers', 'managers')
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            # Check if user is authenticated
            if not request.user.is_authenticated:
                if message:
                    messages.warning(request, message)
                else:
                    messages.warning(request, 'Please log in to access this page.')
                return redirect(redirect_url)
            
            # Check if user belongs to any of the required groups
            user_groups = request.user.groups.values_list('name', flat=True)
            if not any(group in user_groups for group in group_names):
                if message:
                    messages.error(request, message)
                else:
                    groups_str = ', '.join(group_names)
                    messages.error(request, f'Access denied. Required groups: {groups_str}')
                raise PermissionDenied(f"Group access required: {group_names}")
            
            return func(request, *args, **kwargs)
        return wrapper
    return decorator


# Class-based view mixins for the same functionality
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy


class StaffRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    Mixin for class-based views requiring staff access
    """
    login_url = reverse_lazy('login')
    permission_denied_message = 'Staff access required.'
    
    def test_func(self):
        return self.request.user.is_staff


class SuperuserRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    Mixin for class-based views requiring superuser access
    """
    login_url = reverse_lazy('login')
    permission_denied_message = 'Admin access required.'
    
    def test_func(self):
        return self.request.user.is_superuser


class GroupRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    """
    Mixin for class-based views requiring group membership
    Set required_groups as a list of group names
    """
    login_url = reverse_lazy('login')
    required_groups = []
    
    def test_func(self):
        if not self.required_groups:
            return True
        user_groups = self.request.user.groups.values_list('name', flat=True)
        return any(group in user_groups for group in self.required_groups)
    
    def get_permission_denied_message(self):
        groups_str = ', '.join(self.required_groups)
        return f'Access denied. Required groups: {groups_str}'