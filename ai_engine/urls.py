from django.urls import path

from .views import (
    AIExplanationsView,
    AIModelStatusView,
    AIPredictionsView,
    AIRecommendationsView,
)

urlpatterns = [
    path('predictions/', AIPredictionsView.as_view(), name='ai-predictions'),
    path('explanations/', AIExplanationsView.as_view(), name='ai-explanations'),
    path('recommendations/', AIRecommendationsView.as_view(), name='ai-recommendations'),
    path('status/', AIModelStatusView.as_view(), name='ai-model-status'),
]
