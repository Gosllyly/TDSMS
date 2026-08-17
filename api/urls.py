from django.urls import path

from api.views.aps_views import (
    ApsCreateView, ApsDeleteView, ApsExportView, ApsInfoView, ApsItemCreateView, ApsItemDeleteView,
    ApsListView, ApsTemplateView,
)
from api.views.auth_views import (
    AdminCreateView, AdminExpireUpdateView, AdminQueryView, AdminStatusUpdateView, LoginView, LogoutView,
)
from api.views.solve_views import (
    SolveLogsView, SolveMatchCheckView, SolveQueryView, SolveResultView, SolveStartView, SolveStopView,
)
from api.views.task_views import (
    TaskDeleteView, TaskDetailFilterOptionsView, TaskDetailView, TaskHistoryImportView, TaskHistoryView,
    TaskImportView, TaskTemplateView,
)

urlpatterns = [
    path("auth/login", LoginView.as_view()),
    path("auth/logout", LogoutView.as_view()),
    path("aps/template", ApsTemplateView.as_view()),
    path("aps/listQuery", ApsListView.as_view()),
    path("aps/infoQuery", ApsInfoView.as_view()),
    path("aps/create", ApsCreateView.as_view()),
    path("aps/delete", ApsDeleteView.as_view()),
    path("aps/itemCreate", ApsItemCreateView.as_view()),
    path("aps/itemDelete", ApsItemDeleteView.as_view()),
    path("aps/export", ApsExportView.as_view()),
    path("task/template", TaskTemplateView.as_view()),
    path("task/historyQuery", TaskHistoryView.as_view()),
    path("task/delete", TaskDeleteView.as_view()),
    path("tasks/historyImport", TaskHistoryImportView.as_view()),
    path("task/import", TaskImportView.as_view()),
    path("task/detailQuery", TaskDetailView.as_view()),
    path("task/detailFilterOptions", TaskDetailFilterOptionsView.as_view()),
    path("solve/start", SolveStartView.as_view()),
    path("solve/matchCheck", SolveMatchCheckView.as_view()),
    path("solve/query", SolveQueryView.as_view()),
    path("solve/logs", SolveLogsView.as_view()),
    path("solve/stop", SolveStopView.as_view()),
    path("solve/result", SolveResultView.as_view()),
    path("admin/create", AdminCreateView.as_view()),
    path("admin/query", AdminQueryView.as_view()),
    path("admin/expireUpdate", AdminExpireUpdateView.as_view()),
    path("admin/statusUpdate", AdminStatusUpdateView.as_view()),
]
