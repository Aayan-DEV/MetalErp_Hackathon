from django.contrib import admin
from .models import Material, Delivery, ShelfSlot


@admin.register(Material)
class MaterialAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'category', 'created_at')
    search_fields = ('name', 'category')


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ('id', 'manufacturer', 'date', 'size', 'batch_id', 'quantity', 'shelf_id', 'status', 'material')
    list_filter = ('status', 'manufacturer')
    search_fields = ('batch_id', 'manufacturer')


@admin.register(ShelfSlot)
class ShelfSlotAdmin(admin.ModelAdmin):
    list_display = ('shelf_id', 'slot_index', 'is_occupied', 'delivery')
    list_filter = ('is_occupied',)
    search_fields = ('shelf_id',)
