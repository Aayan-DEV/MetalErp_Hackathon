from django.db import models


class Material(models.Model):
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.name} ({self.category})'


class Delivery(models.Model):
    STATUS_CHOICES = [('pending', 'Pending'), ('stored', 'Stored')]

    manufacturer = models.CharField(max_length=200)
    date = models.DateField()
    size = models.CharField(max_length=100)
    batch_id = models.CharField(max_length=50, unique=True)
    quantity = models.CharField(max_length=50)
    shelf_id = models.CharField(max_length=20)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    material = models.ForeignKey(Material, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-date', '-id']

    def __str__(self):
        return f'Delivery #{self.id} — {self.manufacturer} ({self.batch_id})'


class ShelfSlot(models.Model):
    shelf_id = models.CharField(max_length=20)
    slot_index = models.IntegerField()
    is_occupied = models.BooleanField(default=False)
    delivery = models.ForeignKey(Delivery, null=True, blank=True, on_delete=models.SET_NULL, related_name='slots')

    class Meta:
        unique_together = ('shelf_id', 'slot_index')
        ordering = ['shelf_id', 'slot_index']

    def __str__(self):
        status = 'occupied' if self.is_occupied else 'empty'
        return f'{self.shelf_id} slot {self.slot_index} ({status})'
