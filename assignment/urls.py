from django.urls import path
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularSwaggerView,
)
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

urlpatterns = [
    path("schema", SpectacularAPIView.as_view(), name="schema"),
    path("docs", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("auth/signup", views.SignupView.as_view(), name="signup"),
    path("auth/login", views.RoleTokenObtainPairView.as_view(), name="login"),
    path("auth/refresh", TokenRefreshView.as_view(), name="refresh"),
    path("tasks/", views.TaskCreateView.as_view(), name="task-create"),
    path("tasks/<int:pk>", views.TaskDetailView.as_view(), name="task-detail"),
    path("tasks/<int:pk>/eligible-users", views.EligibleUsersView.as_view(),
         name="eligible-users"),
    path("tasks/<int:pk>/complete", views.CompleteTaskView.as_view(),
         name="task-complete"),
    path("tasks/recompute-eligibility", views.RecomputeEligibilityView.as_view(),
         name="recompute-eligibility"),
    path("my-eligible-tasks", views.MyEligibleTasksView.as_view(), name="my-tasks"),
]
