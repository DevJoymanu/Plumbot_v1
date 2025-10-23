from django.shortcuts import render, redirect
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.forms import UserCreationForm, AuthenticationForm
from django.urls import reverse_lazy
from django.views.generic import CreateView
from django.http import HttpResponseRedirect
from django.conf import settings
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from .decorators import admin_required, superuser_required
import logging

logger = logging.getLogger(__name__)


@sensitive_post_parameters()
@csrf_protect
@never_cache
def login_view(request):
    """
    Custom login view with enhanced security and logging
    """
    if request.user.is_authenticated:
        return redirect('dashboard/')
    
    if request.method == 'POST':
        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            username = form.cleaned_data.get('username')
            password = form.cleaned_data.get('password')
            user = authenticate(username=username, password=password)
            
            if user is not None:
                # Check if user is staff (required for this system)
                if not user.is_staff:
                    messages.error(
                        request, 
                        'Access denied. Staff privileges required.'
                    )
                    logger.warning(f"Non-staff user {username} attempted login")
                    return render(request, 'registration/login.html', {'form': form})
                
                login(request, user)
                messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
                
                # Log successful login
                logger.info(f"User {username} logged in successfully")
                
                # Redirect to next page or dashboard
                next_page = request.GET.get('next', 'dashboard/')
                return redirect(next_page)
            else:
                messages.error(request, 'Invalid username or password.')
                logger.warning(f"Failed login attempt for username: {username}")
        else:
            messages.error(request, 'Please correct the errors below.')
    else:
        form = AuthenticationForm()
    
    return render(request, 'registration/login.html', {
        'form': form,
        'title': 'Login - Plumbing Management System'
    })


@login_required
def logout_view(request):
    """
    Custom logout view with logging
    """
    username = request.user.username
    logout(request)
    messages.success(request, 'You have been logged out successfully.')
    logger.info(f"User {username} logged out")
    return redirect('login')


class StaffUserCreationForm(UserCreationForm):
    """
    Custom user creation form that creates staff users by default
    """
    class Meta:
        model = User
        fields = ("username", "email", "first_name", "last_name")
    
    def save(self, commit=True):
        user = super().save(commit=False)
        user.is_staff = True  # Make new users staff by default
        if commit:
            user.save()
        return user


@method_decorator([superuser_required, csrf_protect], name='dispatch')
class CreateUserView(CreateView):
    """
    View for creating new staff users - only accessible to superusers
    """
    form_class = StaffUserCreationForm
    template_name = 'registration/create_user.html'
    success_url = reverse_lazy('user_management')
    
    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(
            self.request, 
            f'User {self.object.username} created successfully.'
        )
        logger.info(f"New user {self.object.username} created by {self.request.user.username}")
        return response


@superuser_required
def user_management_view(request):
    """
    User management dashboard - only accessible to superusers
    """
    users = User.objects.filter(is_staff=True).order_by('-date_joined')
    
    context = {
        'users': users,
        'total_users': users.count(),
        'active_users': users.filter(is_active=True).count(),
        'title': 'User Management'
    }
    
    return render(request, 'registration/user_management.html', context)


@superuser_required
def toggle_user_status(request, user_id):
    """
    Toggle user active status - only accessible to superusers
    """
    try:
        user = User.objects.get(id=user_id, is_staff=True)
        
        # Prevent deactivating the last superuser
        if user.is_superuser and User.objects.filter(is_superuser=True, is_active=True).count() == 1:
            messages.error(request, 'Cannot deactivate the last superuser.')
            return redirect('user_management')
        
        user.is_active = not user.is_active
        user.save()
        
        status = "activated" if user.is_active else "deactivated"
        messages.success(request, f'User {user.username} has been {status}.')
        logger.info(f"User {user.username} {status} by {request.user.username}")
        
    except User.DoesNotExist:
        messages.error(request, 'User not found.')
    
    return redirect('user_management')


@superuser_required
def promote_to_superuser(request, user_id):
    """
    Promote user to superuser - only accessible to superusers
    """
    try:
        user = User.objects.get(id=user_id, is_staff=True)
        
        if not user.is_superuser:
            user.is_superuser = True
            user.save()
            messages.success(request, f'User {user.username} promoted to superuser.')
            logger.info(f"User {user.username} promoted to superuser by {request.user.username}")
        else:
            messages.info(request, f'User {user.username} is already a superuser.')
    
    except User.DoesNotExist:
        messages.error(request, 'User not found.')
    
    return redirect('user_management')


@login_required
def change_password_view(request):
    """
    Simple change password view
    """
    from django.contrib.auth.forms import PasswordChangeForm
    
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            from django.contrib.auth import update_session_auth_hash
            update_session_auth_hash(request, user)  # Important!
            messages.success(request, 'Your password was successfully updated!')
            logger.info(f"User {request.user.username} changed password")
            return redirect('dashboard/')
        else:
            messages.error(request, 'Please correct the error below.')
    else:
        form = PasswordChangeForm(request.user)
    
    return render(request, 'registration/change_password.html', {
        'form': form,
        'title': 'Change Password'
    })


@login_required
def profile_view(request):
    """
    User profile view
    """
    user = request.user
    
    if request.method == 'POST':
        # Simple profile update
        user.first_name = request.POST.get('first_name', user.first_name)
        user.last_name = request.POST.get('last_name', user.last_name)
        user.email = request.POST.get('email', user.email)
        user.save()
        
        messages.success(request, 'Profile updated successfully!')
        logger.info(f"User {user.username} updated profile")
        return redirect('profile')
    
    context = {
        'user': user,
        'title': 'My Profile'
    }
    
    return render(request, 'registration/profile.html', context)


def access_denied_view(request, exception=None):
    """
    Custom 403 access denied view
    """
    return render(request, 'registration/access_denied.html', {
        'title': 'Access Denied',
        'message': 'You do not have permission to access this page.'
    }, status=403)


# Utility function to check if initial setup is needed
def initial_setup_required():
    """
    Check if the system needs initial setup (no superuser exists)
    """
    return not User.objects.filter(is_superuser=True).exists()


def initial_setup_view(request):
    """
    Initial setup view to create the first superuser
    """
    if not initial_setup_required():
        return redirect('login')
    
    if request.method == 'POST':
        form = StaffUserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            user.is_superuser = True
            user.save()
            
            messages.success(
                request, 
                'Initial setup complete! You can now log in as a superuser.'
            )
            logger.info(f"Initial superuser {user.username} created")
            return redirect('login')
    else:
        form = StaffUserCreationForm()
    
    return render(request, 'registration/initial_setup.html', {
        'form': form,
        'title': 'Initial Setup - Create Superuser'
    })


# Session security functions
@login_required
def active_sessions_view(request):
    """
    View active sessions for the current user
    Note: This is a basic implementation. For full session management,
    consider using django-user-sessions
    """
    from django.contrib.sessions.models import Session
    from django.utils import timezone
    
    # Get current user's sessions (basic implementation)
    user_sessions = []
    current_session_key = request.session.session_key
    
    # This is a simplified version - in practice, you'd need to store
    # additional session metadata to properly track user sessions
    
    context = {
        'current_session': current_session_key,
        'sessions': user_sessions,
        'title': 'Active Sessions'
    }
    
    return render(request, 'registration/active_sessions.html', context)


@login_required
def security_log_view(request):
    """
    View security-related logs for the current user
    """
    # This would integrate with your logging system
    # For now, just return a template
    context = {
        'title': 'Security Log',
        'logs': []  # Would be populated from your log files/database
    }
    
    return render(request, 'registration/security_log.html', context)