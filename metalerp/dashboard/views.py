import json
import random
from datetime import date
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_GET, require_POST
from django.db.models import Count
from .models import Material, Delivery, ShelfSlot

# =============================================
# Warehouse Hierarchy Constants
# =============================================
SECTORS = list(range(1, 8))           # Sectors 1-7
UNITS = ['A', 'B', 'C', 'D']         # Units per sector
SHELVES_PER_UNIT = 6                  # Shelves per unit
SLOTS_PER_SHELF = 4                   # 1 row x 4 cols

# Delivery generation data
MANUFACTURERS = [
    'Tata Steel Ltd.', 'JSW Steel', 'SAIL', 'Hindalco Industries',
    'Vedanta Resources', 'Jindal Stainless',
]
MATERIAL_SIZES = [
    '2.5mm x 1250mm', '1.2mm x 1000mm', '12mm dia', '3mm x 1500mm',
    '25kg blocks', '0.5mm x 1250mm', '2mm x 1220mm', '8mm dia',
    '50x50x6mm', '10mm x 2000mm', '1.6mm x 1250mm', '50kg blocks',
    '0.8mm x 1000mm', '1.5mm x 1220mm', '16mm dia', '6mm x 2000mm',
    '4mm x 1500mm', '3.15mm x 1250mm',
]


def _get_shelf_capacity(shelf_id):
    slots = ShelfSlot.objects.filter(shelf_id=shelf_id, is_occupied=True).select_related('delivery')
    occupied_slots = sorted([s.slot_index for s in slots])
    occupied_count = len(occupied_slots)
    percentage = round(occupied_count / SLOTS_PER_SHELF * 100)
    available = [i for i in range(SLOTS_PER_SHELF) if i not in occupied_slots]

    # Track recently stored slots (within last 5 minutes)
    from django.utils import timezone
    import datetime
    recent_cutoff = timezone.now() - datetime.timedelta(minutes=5)
    recently_stored = []
    for s in slots:
        if s.delivery and s.delivery.status == 'stored' and s.delivery.created_at >= recent_cutoff:
            recently_stored.append(s.slot_index)

    return {
        'total_slots': SLOTS_PER_SHELF,
        'occupied_slots': occupied_slots,
        'occupied_count': occupied_count,
        'percentage': percentage,
        'next_available': available[0] if available else None,
        'recently_stored': recently_stored,
    }


def _overall_utilization():
    total_slots = len(SECTORS) * len(UNITS) * SHELVES_PER_UNIT * SLOTS_PER_SHELF
    occupied = ShelfSlot.objects.filter(is_occupied=True).count()
    return round(occupied / total_slots * 100, 1) if total_slots > 0 else 0


# =============================================
# API Endpoints
# =============================================

@require_GET
def shelf_info(request):
    shelf_id = request.GET.get('shelf_id', '')
    if not shelf_id:
        return JsonResponse({'error': 'shelf_id required'}, status=400)
    parts = shelf_id.split('-')
    if len(parts) != 3:
        return JsonResponse({'error': 'Invalid shelf_id format. Use Sector-Unit-Shelf (e.g. 3-B-2)'}, status=400)
    cap = _get_shelf_capacity(shelf_id)
    return JsonResponse({
        'shelf_id': shelf_id,
        'sector': parts[0],
        'unit': parts[1],
        'shelf': parts[2],
        **cap,
    })


@require_POST
def mark_stored(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    shelf_id = data.get('shelf_id', '')
    slot_index = data.get('slot_index')
    if not shelf_id or slot_index is None:
        return JsonResponse({'error': 'shelf_id and slot_index required'}, status=400)

    slot_index = int(slot_index)
    delivery_id = data.get('delivery_id')

    # Find the delivery to link
    delivery_obj = None
    if delivery_id:
        try:
            delivery_obj = Delivery.objects.get(id=int(delivery_id))
        except (Delivery.DoesNotExist, ValueError):
            pass
    else:
        # Auto-find a pending delivery for this shelf
        delivery_obj = Delivery.objects.filter(shelf_id=shelf_id, status='pending').first()

    slot, _ = ShelfSlot.objects.get_or_create(
        shelf_id=shelf_id, slot_index=slot_index,
        defaults={'is_occupied': True, 'delivery': delivery_obj}
    )
    if not slot.is_occupied:
        slot.is_occupied = True
        slot.delivery = delivery_obj
        slot.save()

    # Update delivery status to stored
    if delivery_obj and delivery_obj.status == 'pending':
        delivery_obj.status = 'stored'
        delivery_obj.save()

    cap = _get_shelf_capacity(shelf_id)
    return JsonResponse({
        'shelf_id': shelf_id,
        'stored_slot': slot_index,
        **cap,
    })


@require_GET
def generate_delivery(request):
    manufacturer = random.choice(MANUFACTURERS)
    prefix = ''.join([w[0] for w in manufacturer.split()[:2]]).upper()
    batch_id = f'BATCH-{prefix}-{random.randint(1000, 9999)}'
    size = random.choice(MATERIAL_SIZES)
    quantity = f'{random.randint(10, 120)} MT'
    sector = random.randint(1, 7)
    unit = random.choice(UNITS)
    shelf = random.randint(1, SHELVES_PER_UNIT)
    shelf_id = f'{sector}-{unit}-{shelf}'

    # Pick a random material
    material = Material.objects.order_by('?').first()
    material_id = material.id if material else None
    material_name = material.name if material else ''

    return JsonResponse({
        'manufacturer': manufacturer,
        'date': str(date.today()),
        'size': size,
        'batch_id': batch_id,
        'quantity': quantity,
        'shelf_id': shelf_id,
        'material_id': material_id,
        'material_name': material_name,
    })


@require_POST
def add_delivery(request):
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON'}, status=400)
    required = ['manufacturer', 'date', 'size', 'batch_id', 'quantity', 'shelf_id']
    for field in required:
        if field not in data or not data[field]:
            return JsonResponse({'error': f'{field} is required'}, status=400)

    # Link material if provided
    material = None
    material_id_input = data.get('material_id')
    if material_id_input:
        try:
            material = Material.objects.get(id=int(material_id_input))
        except (Material.DoesNotExist, ValueError):
            pass

    delivery = Delivery.objects.create(
        manufacturer=data['manufacturer'],
        date=data['date'],
        size=data['size'],
        batch_id=data['batch_id'],
        quantity=data['quantity'],
        shelf_id=data['shelf_id'],
        status='pending',
        material=material,
    )

    return JsonResponse({
        'id': delivery.id,
        'manufacturer': delivery.manufacturer,
        'date': str(delivery.date),
        'size': delivery.size,
        'batch_id': delivery.batch_id,
        'quantity': delivery.quantity,
        'shelf_id': delivery.shelf_id,
        'status': delivery.status,
        'material_id': delivery.material.id if delivery.material else None,
        'material_name': delivery.material.name if delivery.material else '',
    })


# =============================================
# Page Views
# =============================================

def index(request):
    context = {
        'active_tab': 'dashboard',
        'stats': {
            'total_materials': Material.objects.count(),
            'pending_deliveries': Delivery.objects.filter(status='pending').count(),
            'active_manufacturing': 3,
            'completed_orders': 2,
            'storage_utilization': _overall_utilization(),
        },
    }
    return render(request, 'dashboard/index.html', context)


def delivery(request):
    deliveries = Delivery.objects.select_related('material').all()
    delivery_list = []
    for d in deliveries:
        delivery_list.append({
            'id': d.id,
            'manufacturer': d.manufacturer,
            'date': str(d.date),
            'size': d.size,
            'batch_id': d.batch_id,
            'quantity': d.quantity,
            'shelf_id': d.shelf_id,
            'status': d.status,
            'material_id': d.material.id if d.material else None,
            'material_name': d.material.name if d.material else '',
        })
    context = {
        'active_tab': 'delivery',
        'slots_per_shelf': SLOTS_PER_SHELF,
        'deliveries': delivery_list,
    }
    return render(request, 'dashboard/delivery.html', context)


def manufacturing(request):
    context = {
        'active_tab': 'manufacturing',
        'work_orders': [
            {'id': 'WO-001', 'product': 'HR Coil 2.5mm',  'batch': 'BATCH-MFG-101', 'start': '2026-03-25', 'status': 'In Progress', 'status_class': 'in-progress', 'completion': 72, 'shelf_id': None},
            {'id': 'WO-002', 'product': 'CR Sheet 1.2mm',  'batch': 'BATCH-MFG-102', 'start': '2026-03-24', 'status': 'In Progress', 'status_class': 'in-progress', 'completion': 45, 'shelf_id': None},
            {'id': 'WO-003', 'product': 'GP Sheet 0.5mm',  'batch': 'BATCH-MFG-103', 'start': '2026-03-24', 'status': 'Queued',      'status_class': 'queued',      'completion': 0,  'shelf_id': None},
            {'id': 'WO-004', 'product': 'TMT Bar Fe-500',  'batch': 'BATCH-MFG-104', 'start': '2026-03-23', 'status': 'Completed',   'status_class': 'completed',   'completion': 100,'shelf_id': '3-A-4'},
            {'id': 'WO-005', 'product': 'MS Angle 50x50',  'batch': 'BATCH-MFG-105', 'start': '2026-03-23', 'status': 'In Progress', 'status_class': 'in-progress', 'completion': 88, 'shelf_id': None},
            {'id': 'WO-006', 'product': 'HR Plate 10mm',   'batch': 'BATCH-MFG-106', 'start': '2026-03-22', 'status': 'Completed',   'status_class': 'completed',   'completion': 100,'shelf_id': '6-D-3'},
        ],
    }
    return render(request, 'dashboard/manufacturing.html', context)


def materials(request):
    mats = Material.objects.annotate(delivery_count=Count('delivery')).order_by('id')
    # Compute total quantity per material from linked deliveries
    materials_list = []
    for m in mats:
        total_qty = 0
        for d in m.delivery_set.all():
            # quantity is like "42 MT" — extract numeric part
            try:
                total_qty += int(''.join(c for c in d.quantity.split()[0] if c.isdigit()))
            except (ValueError, IndexError):
                pass
        materials_list.append({
            'id': m.id,
            'name': m.name,
            'category': m.category,
            'delivery_count': m.delivery_count,
            'total_quantity': f'{total_qty} MT' if total_qty > 0 else '—',
        })
    context = {
        'active_tab': 'materials',
        'materials': materials_list,
    }
    return render(request, 'dashboard/materials.html', context)
