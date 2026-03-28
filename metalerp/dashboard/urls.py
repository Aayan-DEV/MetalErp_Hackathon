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
    path('api/warehouse-map/', views.warehouse_map, name='warehouse_map'),
    path('api/warehouse-stats/', views.warehouse_stats, name='warehouse_stats'),
    path('api/delivery-statuses/', views.delivery_statuses, name='delivery_statuses'),
    path('api/delete-delivery/', views.delete_delivery, name='delete_delivery'),
    path('api/deleted-deliveries/', views.deleted_deliveries, name='deleted_deliveries'),
    path('api/save-manufacturing-order/', views.save_manufacturing_order, name='save_manufacturing_order'),
    path('api/consume-pallet/', views.consume_pallet, name='consume_pallet'),
    # Logs
    path('logs/', views.logs, name='logs'),
    # Machine Health
    path('health/', views.health, name='health'),
    path('api/machine-health-data/', views.machine_health_data, name='machine_health_data'),
    path('api/update-failure-threshold/', views.update_failure_threshold, name='update_failure_threshold'),
    path('api/reset-machine/', views.reset_machine, name='reset_machine'),
    path('api/increment-machine-usage/', views.increment_machine_usage, name='increment_machine_usage'),
    path('api/update-machine-detail/', views.update_machine_detail, name='update_machine_detail'),
    path('api/add-machine/', views.add_machine, name='add_machine'),
    path('api/delete-machine/', views.delete_machine, name='delete_machine'),
    path('api/reorder-machines/', views.reorder_machines, name='reorder_machines'),
    # Warehouse selection
    path('api/warehouses/', views.warehouse_list, name='warehouse_list'),
    path('api/set-warehouse/', views.set_warehouse, name='set_warehouse'),
]
