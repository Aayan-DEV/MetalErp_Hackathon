from django.urls import path
from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.index, name='index'),
    path('delivery/', views.delivery, name='delivery'),
    path('manufacturing/', views.manufacturing, name='manufacturing'),
    path('materials/', views.materials, name='materials'),
    path('api/shelf-info/', views.shelf_info, name='shelf_info'),
    path('api/mark-stored/', views.mark_stored, name='mark_stored'),
    path('api/generate-delivery/', views.generate_delivery, name='generate_delivery'),
    path('api/add-delivery/', views.add_delivery, name='add_delivery'),
]
