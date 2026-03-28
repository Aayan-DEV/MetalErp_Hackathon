"""
AI tool definitions and execution logic for the MetalERP chat assistant.
All tools are read-only database queries across the 9 models.
"""
import json
import math
from datetime import datetime, date, timedelta
from collections import defaultdict
from django.db.models import Count, Sum, Q, F, Avg
from django.utils import timezone
from .models import (
    Warehouse, Material, Delivery, ManufacturingOrder,
    MachineHealth, ScrapEvent, ShelfSlot, WarehouseCell, GlobalLog,
    MaintenanceEntry,
)


def _serialize(obj):
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return str(obj)


def _parse_date(value):
    """Parse YYYY-MM-DD string to date object. Returns None on failure."""
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value).strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _to_json(data, max_chars=8000):
    raw = json.dumps(data, default=_serialize, ensure_ascii=False)
    if len(raw) > max_chars:
        return raw[:max_chars] + '\n... (truncated)'
    return raw


# ── Tool definitions for the Anthropic API ──────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "search_deliveries",
        "description": "Search deliveries in the ERP system. Can filter by manufacturer, status, batch ID, material name, and date range. Returns delivery details including warehouse, material, shelf location, and quantity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "manufacturer": {"type": "string", "description": "Filter by manufacturer name (partial match)"},
                "status": {"type": "string", "enum": ["pending", "stored", "deleted"], "description": "Filter by delivery status"},
                "batch_id": {"type": "string", "description": "Filter by batch ID (partial match)"},
                "material_name": {"type": "string", "description": "Filter by material name (partial match)"},
                "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)"},
                "warehouse_code": {"type": "string", "description": "Filter by warehouse code"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": [],
        },
    },
    {
        "name": "search_manufacturing_orders",
        "description": "Search manufacturing/work orders. Can filter by product, status, quality, order ID, and material. Returns order details including processing time, energy, scrap rate, defect info, and stage data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Filter by product name (partial match)"},
                "status": {"type": "string", "enum": ["completed", "defected"], "description": "Filter by order status"},
                "quality": {"type": "string", "enum": ["PASS", "FAIL"], "description": "Filter by quality result"},
                "order_id": {"type": "string", "description": "Filter by order ID (partial match)"},
                "material_name": {"type": "string", "description": "Filter by material name (partial match)"},
                "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_machine_health",
        "description": "Get machine health information. If machine_id or machine_name is provided, returns detailed info for that machine including resources, maintenance log, and parts. Otherwise returns a summary of all machines.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Specific machine ID (e.g. MCH-UL-01)"},
                "machine_name": {"type": "string", "description": "Machine name (partial match)"},
            },
            "required": [],
        },
    },
    {
        "name": "search_materials",
        "description": "Search materials in the inventory. Returns material info with delivery counts, total quantities, and storage locations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filter by material name (partial match)"},
                "category": {"type": "string", "description": "Filter by category (partial match)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_warehouse_stats",
        "description": "Get warehouse capacity and utilization statistics. Returns total slots, occupied slots, utilization percentage, and delivery counts.",
        "input_schema": {
            "type": "object",
            "properties": {
                "warehouse_code": {"type": "string", "description": "Specific warehouse code. If omitted, returns stats for all warehouses."},
            },
            "required": [],
        },
    },
    {
        "name": "search_logs",
        "description": "Search the global event/audit log. Can filter by event type, severity, text search, and date range.",
        "input_schema": {
            "type": "object",
            "properties": {
                "event_type": {"type": "string", "enum": ["delivery", "manufacturing", "scrap", "machine", "material", "warehouse", "shipment", "threshold"], "description": "Filter by event type"},
                "severity": {"type": "string", "enum": ["info", "warning", "error", "critical"], "description": "Filter by severity"},
                "search": {"type": "string", "description": "Text search in title and description"},
                "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)"},
                "limit": {"type": "integer", "description": "Max results (default 30)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_scrap_events",
        "description": "Search scrap/waste events from manufacturing. Can filter by machine, order, or scrap type.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_name": {"type": "string", "description": "Filter by machine name (partial match)"},
                "machine_id": {"type": "string", "description": "Filter by machine ID"},
                "order_id": {"type": "string", "description": "Filter by manufacturing order ID (partial match)"},
                "scrap_type": {"type": "string", "description": "Filter by scrap type (partial match)"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": [],
        },
    },
    {
        "name": "get_dashboard_summary",
        "description": "Get a high-level summary of the entire ERP system: delivery counts by status, order counts by status/quality, warehouse utilization, machine health overview, and recent log activity. Use this for broad overview questions.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ── Tool execution functions ────────────────────────────────────────────

def _search_deliveries(params):
    qs = Delivery.objects.select_related('warehouse', 'material')
    if v := params.get('manufacturer'):
        qs = qs.filter(manufacturer__icontains=v)
    if v := params.get('status'):
        qs = qs.filter(status=v)
    if v := params.get('batch_id'):
        qs = qs.filter(batch_id__icontains=v)
    if v := params.get('material_name'):
        qs = qs.filter(material__name__icontains=v)
    if v := params.get('date_from'):
        qs = qs.filter(date__gte=v)
    if v := params.get('date_to'):
        qs = qs.filter(date__lte=v)
    if v := params.get('warehouse_code'):
        qs = qs.filter(warehouse__code=v)
    limit = min(params.get('limit', 20), 50)
    rows = []
    for d in qs[:limit]:
        rows.append({
            'id': d.id,
            'manufacturer': d.manufacturer,
            'date': d.date,
            'size': d.size,
            'batch_id': d.batch_id,
            'quantity': d.quantity,
            'shelf_id': d.shelf_id,
            'status': d.status,
            'warehouse': d.warehouse.name if d.warehouse else None,
            'material': d.material.name if d.material else None,
            'delete_reason': d.delete_reason or None,
            'created_at': d.created_at,
        })
    return {'count': qs.count(), 'results': rows}


def _search_manufacturing_orders(params):
    qs = ManufacturingOrder.objects.select_related('material', 'delivery')
    if v := params.get('product'):
        qs = qs.filter(product__icontains=v)
    if v := params.get('status'):
        qs = qs.filter(status=v)
    if v := params.get('quality'):
        qs = qs.filter(quality=v)
    if v := params.get('order_id'):
        qs = qs.filter(order_id__icontains=v)
    if v := params.get('material_name'):
        qs = qs.filter(material_name__icontains=v)
    if v := params.get('date_from'):
        qs = qs.filter(created_at__date__gte=v)
    if v := params.get('date_to'):
        qs = qs.filter(created_at__date__lte=v)
    limit = min(params.get('limit', 20), 50)
    rows = []
    for o in qs[:limit]:
        rows.append({
            'order_id': o.order_id,
            'product': o.product,
            'dimensions': o.dimensions,
            'material': o.material_name or (o.material.name if o.material else None),
            'delivery_batch': o.delivery_batch,
            'manufacturer': o.manufacturer,
            'status': o.status,
            'quality': o.quality,
            'processing_time_sec': o.processing_time,
            'total_energy_kwh': o.total_energy,
            'total_scrap_pct': o.total_scrap,
            'defect_machine': o.defect_machine or None,
            'defect_type': o.defect_type or None,
            'defect_cause': o.defect_cause or None,
            'stages_completed': o.stages_completed,
            'created_at': o.created_at,
        })
    return {'count': qs.count(), 'results': rows}


def _compute_health(usage, threshold):
    if threshold <= 0:
        return 0.0
    ratio = usage / threshold
    import math
    fail_prob = 1 - math.exp(-((ratio) ** 3.5))
    return round(max(0, (1 - fail_prob)) * 100, 1)


def _get_machine_health(params):
    qs = MachineHealth.objects.all()
    if v := params.get('machine_id'):
        qs = qs.filter(machine_id=v)
    if v := params.get('machine_name'):
        qs = qs.filter(machine_name__icontains=v)

    rows = []
    for m in qs:
        health_pct = _compute_health(m.usage_count, m.failure_threshold)
        entry = {
            'machine_id': m.machine_id,
            'machine_name': m.machine_name,
            'usage_count': m.usage_count,
            'failure_threshold': m.failure_threshold,
            'health_pct': health_pct,
            'position': m.position,
            'last_maintenance': m.last_maintenance,
            'updated_at': m.updated_at,
        }
        if qs.count() <= 3:
            entry['detail_data'] = m.detail_data
        rows.append(entry)
    return {'count': len(rows), 'machines': rows}


def _search_materials(params):
    qs = Material.objects.all()
    if v := params.get('name'):
        qs = qs.filter(name__icontains=v)
    if v := params.get('category'):
        qs = qs.filter(category__icontains=v)
    rows = []
    for m in qs:
        deliveries = Delivery.objects.filter(material=m).exclude(status='deleted')
        total_qty = 0
        for d in deliveries:
            try:
                total_qty += int(''.join(c for c in str(d.quantity) if c.isdigit()) or '0')
            except ValueError:
                pass
        locations = list(
            ShelfSlot.objects.filter(delivery__material=m, is_occupied=True)
            .values_list('shelf_id', flat=True).distinct()[:10]
        )
        rows.append({
            'id': m.id,
            'name': m.name,
            'category': m.category,
            'delivery_count': deliveries.count(),
            'total_quantity': total_qty,
            'storage_locations': locations,
        })
    return {'count': len(rows), 'materials': rows}


def _get_warehouse_stats(params):
    if v := params.get('warehouse_code'):
        warehouses = Warehouse.objects.filter(code=v)
    else:
        warehouses = Warehouse.objects.all()

    results = []
    for w in warehouses:
        total = ShelfSlot.objects.filter(warehouse=w).count()
        occupied = ShelfSlot.objects.filter(warehouse=w, is_occupied=True).count()
        pending = Delivery.objects.filter(warehouse=w, status='pending').count()
        stored = Delivery.objects.filter(warehouse=w, status='stored').count()
        results.append({
            'warehouse': w.name,
            'code': w.code,
            'total_slots': total,
            'occupied_slots': occupied,
            'available_slots': total - occupied,
            'utilization_pct': round(occupied / total * 100, 1) if total else 0,
            'pending_deliveries': pending,
            'stored_deliveries': stored,
        })
    return {'warehouses': results}


def _search_logs(params):
    qs = GlobalLog.objects.select_related('delivery', 'manufacturing_order', 'machine', 'scrap_event')
    if v := params.get('event_type'):
        qs = qs.filter(event_type=v)
    if v := params.get('severity'):
        qs = qs.filter(severity=v)
    if v := params.get('search'):
        qs = qs.filter(Q(title__icontains=v) | Q(description__icontains=v))
    if v := params.get('date_from'):
        qs = qs.filter(timestamp__date__gte=v)
    if v := params.get('date_to'):
        qs = qs.filter(timestamp__date__lte=v)
    limit = min(params.get('limit', 30), 100)
    rows = []
    for log in qs[:limit]:
        rows.append({
            'timestamp': log.timestamp,
            'event_type': log.event_type,
            'severity': log.severity,
            'title': log.title,
            'description': log.description[:200] if log.description else None,
            'related_delivery': log.delivery_id,
            'related_order': log.manufacturing_order.order_id if log.manufacturing_order else None,
            'related_machine': log.machine.machine_id if log.machine else None,
        })
    return {'count': qs.count(), 'results': rows}


def _get_scrap_events(params):
    qs = ScrapEvent.objects.select_related('order')
    if v := params.get('machine_name'):
        qs = qs.filter(machine_name__icontains=v)
    if v := params.get('machine_id'):
        qs = qs.filter(machine_id=v)
    if v := params.get('order_id'):
        qs = qs.filter(order__order_id__icontains=v)
    if v := params.get('scrap_type'):
        qs = qs.filter(scrap_type__icontains=v)
    limit = min(params.get('limit', 20), 50)
    rows = []
    for s in qs[:limit]:
        rows.append({
            'order_id': s.order.order_id,
            'machine_name': s.machine_name,
            'machine_id': s.machine_id,
            'scrap_type': s.scrap_type,
            'scrap_rate_pct': s.scrap_rate,
            'material_name': s.material_name or None,
            'delivery_batch': s.delivery_batch or None,
            'created_at': s.created_at,
        })
    return {'count': qs.count(), 'results': rows}


def _get_dashboard_summary(params):
    return {
        'deliveries': {
            'pending': Delivery.objects.filter(status='pending').count(),
            'stored': Delivery.objects.filter(status='stored').count(),
            'deleted': Delivery.objects.filter(status='deleted').count(),
            'total': Delivery.objects.count(),
        },
        'manufacturing_orders': {
            'completed': ManufacturingOrder.objects.filter(status='completed').count(),
            'defected': ManufacturingOrder.objects.filter(status='defected').count(),
            'pass': ManufacturingOrder.objects.filter(quality='PASS').count(),
            'fail': ManufacturingOrder.objects.filter(quality='FAIL').count(),
            'total': ManufacturingOrder.objects.count(),
        },
        'materials': {
            'total': Material.objects.count(),
            'categories': list(Material.objects.values_list('category', flat=True).distinct()),
        },
        'machines': {
            'total': MachineHealth.objects.count(),
            'machines': [
                {
                    'id': m.machine_id,
                    'name': m.machine_name,
                    'health_pct': _compute_health(m.usage_count, m.failure_threshold),
                    'usage': m.usage_count,
                    'threshold': m.failure_threshold,
                }
                for m in MachineHealth.objects.all()
            ],
        },
        'warehouses': [
            {
                'name': w.name,
                'code': w.code,
                'utilization_pct': round(
                    ShelfSlot.objects.filter(warehouse=w, is_occupied=True).count()
                    / max(ShelfSlot.objects.filter(warehouse=w).count(), 1) * 100, 1
                ),
            }
            for w in Warehouse.objects.all()
        ],
        'scrap_events_total': ScrapEvent.objects.count(),
        'logs_recent': {
            'info': GlobalLog.objects.filter(severity='info').count(),
            'warning': GlobalLog.objects.filter(severity='warning').count(),
            'error': GlobalLog.objects.filter(severity='error').count(),
            'critical': GlobalLog.objects.filter(severity='critical').count(),
        },
    }


# ── Warehouse Operator Tool Definitions ─────────────────────────────────

WAREHOUSE_OPERATOR_TOOL_DEFINITIONS = [
    {
        "name": "daily_briefing",
        "description": "Get a comprehensive daily briefing for the warehouse operator: pending deliveries, warehouse utilization, arrivals today, recent alerts, and action items. Call this when the user asks 'what should I do?', greets you, or wants an overview of their day.",
        "input_schema": {
            "type": "object",
            "properties": {
                "warehouse_code": {"type": "string", "description": "Optional: filter to a specific warehouse code"},
            },
            "required": [],
        },
    },
    {
        "name": "forklift_route_plan",
        "description": "Plan an optimized forklift route through the warehouse for pending deliveries. Returns an ordered list of stops with shelf locations, materials, and quantities to move. Supports a start_sector to optimize the route from the operator's current location.",
        "input_schema": {
            "type": "object",
            "properties": {
                "warehouse_code": {"type": "string", "description": "Optional: specific warehouse code"},
                "start_sector": {"type": "integer", "description": "The sector number where the operator currently is. Used to optimize the route starting point. Defaults to 1 (dock area)."},
            },
            "required": [],
        },
    },
    {
        "name": "capacity_forecast",
        "description": "Forecast warehouse capacity over the next N days based on historical delivery rates and consumption patterns. Returns per-day utilization projections.",
        "input_schema": {
            "type": "object",
            "properties": {
                "warehouse_code": {"type": "string", "description": "Optional: specific warehouse code"},
                "days_ahead": {"type": "integer", "description": "Number of days to forecast (default 7, max 30)"},
            },
            "required": [],
        },
    },
    {
        "name": "shift_handoff_summary",
        "description": "Generate a shift handoff summary: what happened in the last N hours including deliveries received, items stored, alerts, and any anomalies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours_back": {"type": "integer", "description": "Hours to look back (default 8)"},
            },
            "required": [],
        },
    },
    {
        "name": "priority_queue",
        "description": "Get pending deliveries ranked by processing priority. Considers age (older = more urgent), material demand from manufacturing, and shelf proximity.",
        "input_schema": {
            "type": "object",
            "properties": {
                "warehouse_code": {"type": "string", "description": "Optional: specific warehouse code"},
            },
            "required": [],
        },
    },
    {
        "name": "anomaly_detection",
        "description": "Detect anomalies in warehouse operations: capacity warnings (>90%), unusual delivery volumes, machines near failure threshold, and other operational flags.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days_back": {"type": "integer", "description": "Days to analyze (default 7)"},
            },
            "required": [],
        },
    },
    {
        "name": "store_delivery",
        "description": "Store a delivery — marks all pallets as stored on the assigned shelf. Simulates forklift placement + LiDAR confirmation. You can look up the delivery by batch_id, shelf_id, manufacturer, or material — use whatever the operator gave you. Do NOT ask the operator for a delivery ID — they can't see it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "batch_id": {"type": "string", "description": "Batch ID (e.g. BATCH-JS-2523-8dff). Preferred lookup method."},
                "shelf_id": {"type": "string", "description": "Shelf location (e.g. 2-A-3). Finds the pending delivery assigned to this shelf."},
                "manufacturer": {"type": "string", "description": "Manufacturer name to narrow down the delivery"},
                "material": {"type": "string", "description": "Material name to narrow down the delivery"},
                "warehouse_code": {"type": "string", "description": "Warehouse code to scope the search to the operator's current warehouse. Always pass this."},
                "delivery_id": {"type": "integer", "description": "Internal delivery ID (operators usually don't know this)"},
            },
            "required": [],
        },
    },
]


# ── Warehouse Operator Tool Execution ──────────────────────────────────

def _daily_briefing(params):
    today = date.today()
    wh_filter = {}
    if v := params.get('warehouse_code'):
        wh_filter['warehouse__code'] = v

    pending = Delivery.objects.filter(status='pending', **wh_filter).select_related('warehouse', 'material')
    arriving = Delivery.objects.filter(date=today, **wh_filter)
    stored_today_qs = GlobalLog.objects.filter(
        event_type='delivery', timestamp__date=today,
        title__icontains='stored'
    )

    # Warehouse utilization
    warehouses = Warehouse.objects.filter(code=params['warehouse_code']) if params.get('warehouse_code') else Warehouse.objects.all()
    utilization = []
    for w in warehouses:
        total = ShelfSlot.objects.filter(warehouse=w).count()
        occupied = ShelfSlot.objects.filter(warehouse=w, is_occupied=True).count()
        utilization.append({
            'name': w.name, 'code': w.code,
            'utilization_pct': round(occupied / max(total, 1) * 100, 1),
            'available_slots': total - occupied,
            'total_slots': total,
        })

    # Recent warnings
    alerts = GlobalLog.objects.filter(
        severity__in=['warning', 'critical', 'error'],
        timestamp__gte=timezone.now() - timedelta(hours=24)
    ).order_by('-timestamp')[:5]

    return {
        'date': today.isoformat(),
        'pending_count': pending.count(),
        'pending_deliveries': [{
            'batch_id': d.batch_id, 'manufacturer': d.manufacturer,
            'material': d.material.name if d.material else None,
            'shelf_id': d.shelf_id, 'quantity': d.quantity,
            'date': str(d.date),
        } for d in pending[:15]],
        'received_today': arriving.count(),
        'stored_today': stored_today_qs.count(),
        'warehouse_utilization': utilization,
        'alerts': [{'severity': l.severity, 'title': l.title, 'time': l.timestamp.isoformat()} for l in alerts],
        'total_materials': Material.objects.count(),
    }


def _forklift_route_plan(params):
    wh_filter = {}
    if v := params.get('warehouse_code'):
        wh_filter['warehouse__code'] = v

    pending = list(Delivery.objects.filter(status='pending', **wh_filter).select_related('material'))
    if not pending:
        return {'message': 'No pending deliveries to route.', 'stops': []}

    # Parse shelf_id (Sector-Unit-Shelf) and sort by sector then unit for proximity
    stops = []
    for d in pending:
        parts = d.shelf_id.split('-') if d.shelf_id else ['0', 'A', '0']
        try:
            sector = int(parts[0])
        except (ValueError, IndexError):
            sector = 0
        unit = parts[1] if len(parts) > 1 else 'A'
        shelf = parts[2] if len(parts) > 2 else '0'
        stops.append({
            'delivery_id': d.id,
            'batch_id': d.batch_id,
            'manufacturer': d.manufacturer,
            'material': d.material.name if d.material else None,
            'quantity': d.quantity,
            'shelf_id': d.shelf_id,
            'sector': sector,
            'unit': unit,
            'shelf': shelf,
        })

    # Route optimization: linear sweep from start_sector
    start_sector = params.get('start_sector', 1)
    try:
        start_sector = int(start_sector)
    except (ValueError, TypeError):
        start_sector = 1

    # Group by sector-unit, sort shelves within each group
    groups = {}
    for s in stops:
        key = (s['sector'], s['unit'])
        if key not in groups:
            groups[key] = {'sector': s['sector'], 'unit': s['unit'], 'shelves': []}
        groups[key]['shelves'].append(s)
    for g in groups.values():
        g['shelves'].sort(key=lambda x: x['shelf'])

    all_groups = sorted(groups.values(), key=lambda g: (g['sector'], g['unit']))

    # Find sector range
    sectors = set(g['sector'] for g in all_groups)
    min_sector = min(sectors) if sectors else 1
    max_sector = max(sectors) if sectors else 1

    # Decide direction: go to whichever end is closer first
    dist_to_max = max_sector - start_sector
    dist_to_min = start_sector - min_sector
    go_high_first = dist_to_max <= dist_to_min

    visited = []
    if go_high_first:
        for g in [x for x in all_groups if x['sector'] >= start_sector]:
            visited.extend(g['shelves'])
        for g in sorted([x for x in all_groups if x['sector'] < start_sector],
                        key=lambda x: (-x['sector'], x['unit'])):
            visited.extend(g['shelves'])
    else:
        for g in sorted([x for x in all_groups if x['sector'] <= start_sector],
                        key=lambda x: (-x['sector'], x['unit'])):
            visited.extend(g['shelves'])
        for g in [x for x in all_groups if x['sector'] > start_sector]:
            visited.extend(g['shelves'])

    route = []
    for i, stop in enumerate(visited, 1):
        route.append({
            'stop_number': i,
            'shelf_id': stop['shelf_id'],
            'batch_id': stop.get('batch_id', ''),
            'material': stop['material'],
            'manufacturer': stop['manufacturer'],
            'quantity': stop['quantity'],
        })

    return {
        'total_stops': len(route),
        'start_sector': start_sector,
        'route': route,
        'estimated_description': f'{len(route)} stops across {len(sectors)} sectors, starting from sector {start_sector}',
    }


def _capacity_forecast(params):
    days_ahead = min(params.get('days_ahead', 7), 30)
    today = date.today()

    wh_filter = {}
    if v := params.get('warehouse_code'):
        wh_filter['warehouse__code'] = v

    warehouses = Warehouse.objects.filter(code=params['warehouse_code']) if params.get('warehouse_code') else Warehouse.objects.all()

    forecasts = []
    for w in warehouses:
        total = ShelfSlot.objects.filter(warehouse=w).count()
        occupied = ShelfSlot.objects.filter(warehouse=w, is_occupied=True).count()

        # Calculate avg daily incoming rate (last 14 days)
        lookback = today - timedelta(days=14)
        incoming = Delivery.objects.filter(warehouse=w, date__gte=lookback).count()
        avg_incoming = incoming / 14.0

        # Calculate avg daily consumption (pallets consumed by manufacturing)
        consumed = ManufacturingOrder.objects.filter(
            created_at__date__gte=lookback
        ).count()
        avg_consumed = consumed / 14.0

        net_daily = avg_incoming - avg_consumed
        daily_forecast = []
        current_occupied = occupied
        for day in range(1, days_ahead + 1):
            current_occupied = max(0, min(total, current_occupied + net_daily))
            pct = round(current_occupied / max(total, 1) * 100, 1)
            daily_forecast.append({
                'date': (today + timedelta(days=day)).isoformat(),
                'projected_utilization_pct': pct,
                'projected_occupied': round(current_occupied),
                'projected_available': total - round(current_occupied),
            })

        forecasts.append({
            'warehouse': w.name,
            'code': w.code,
            'current_utilization_pct': round(occupied / max(total, 1) * 100, 1),
            'avg_daily_incoming': round(avg_incoming, 1),
            'avg_daily_consumed': round(avg_consumed, 1),
            'net_daily_change': round(net_daily, 1),
            'forecast': daily_forecast,
        })

    return {'forecasts': forecasts}


def _shift_handoff_summary(params):
    hours_back = min(params.get('hours_back', 8), 24)
    cutoff = timezone.now() - timedelta(hours=hours_back)

    logs = GlobalLog.objects.filter(timestamp__gte=cutoff).order_by('-timestamp')

    by_type = defaultdict(list)
    for log in logs[:50]:
        by_type[log.event_type].append({
            'title': log.title,
            'severity': log.severity,
            'time': log.timestamp.isoformat(),
            'description': (log.description[:150] if log.description else None),
        })

    deliveries_received = Delivery.objects.filter(created_at__gte=cutoff).count()
    deliveries_stored = GlobalLog.objects.filter(
        event_type='delivery', timestamp__gte=cutoff, title__icontains='stored'
    ).count()

    anomalies = list(GlobalLog.objects.filter(
        timestamp__gte=cutoff, severity__in=['warning', 'critical', 'error']
    ).values('severity', 'title', 'timestamp')[:10])

    return {
        'period': f'Last {hours_back} hours',
        'cutoff': cutoff.isoformat(),
        'deliveries_received': deliveries_received,
        'deliveries_stored': deliveries_stored,
        'total_events': logs.count(),
        'events_by_type': {k: len(v) for k, v in by_type.items()},
        'event_details': dict(by_type),
        'anomalies': [{'severity': a['severity'], 'title': a['title'], 'time': a['timestamp'].isoformat()} for a in anomalies],
    }


def _priority_queue(params):
    wh_filter = {}
    if v := params.get('warehouse_code'):
        wh_filter['warehouse__code'] = v

    pending = list(Delivery.objects.filter(status='pending', **wh_filter).select_related('material'))
    if not pending:
        return {'message': 'No pending deliveries.', 'queue': []}

    today = date.today()
    queue = []
    for d in pending:
        age_days = (today - d.date).days if d.date else 0
        # Higher priority score = process first
        priority_score = age_days * 10  # Older deliveries get higher priority

        # Check if material has pending manufacturing orders
        if d.material:
            mfg_demand = ManufacturingOrder.objects.filter(
                material_name__icontains=d.material.name,
                status='completed'
            ).count()
            # More demand = higher priority
            priority_score += mfg_demand * 5

        queue.append({
            'delivery_id': d.id,
            'manufacturer': d.manufacturer,
            'material': d.material.name if d.material else None,
            'quantity': d.quantity,
            'shelf_id': d.shelf_id,
            'date': str(d.date),
            'age_days': age_days,
            'priority_score': priority_score,
        })

    queue.sort(key=lambda x: x['priority_score'], reverse=True)

    # Add rank
    for i, item in enumerate(queue, 1):
        item['rank'] = i

    return {'total': len(queue), 'queue': queue[:20]}


def _anomaly_detection(params):
    days_back = min(params.get('days_back', 7), 30)
    cutoff = date.today() - timedelta(days=days_back)
    anomalies = []

    # 1. Warehouse capacity warnings
    for w in Warehouse.objects.all():
        total = ShelfSlot.objects.filter(warehouse=w).count()
        occupied = ShelfSlot.objects.filter(warehouse=w, is_occupied=True).count()
        pct = round(occupied / max(total, 1) * 100, 1)
        if pct > 90:
            anomalies.append({
                'type': 'capacity_warning',
                'severity': 'critical' if pct > 95 else 'warning',
                'message': f'{w.name} ({w.code}) is at {pct}% capacity — {total - occupied} slots remaining',
                'warehouse': w.code,
            })

    # 2. Unusual delivery volume
    recent_deliveries = Delivery.objects.filter(date__gte=cutoff).count()
    lookback_long = date.today() - timedelta(days=60)
    historical = Delivery.objects.filter(date__gte=lookback_long, date__lt=cutoff)
    if historical.exists():
        hist_count = historical.count()
        hist_days = (cutoff - lookback_long).days or 1
        avg_daily = hist_count / hist_days
        recent_daily = recent_deliveries / max(days_back, 1)
        if avg_daily > 0 and recent_daily > avg_daily * 2:
            anomalies.append({
                'type': 'volume_spike',
                'severity': 'warning',
                'message': f'Delivery volume is {round(recent_daily, 1)}/day vs historical avg of {round(avg_daily, 1)}/day',
            })

    # 3. Machines near failure threshold
    for m in MachineHealth.objects.all():
        ratio = m.usage_count / max(m.failure_threshold, 1)
        if ratio > 0.85:
            health_pct = round(max(0, (1 - (1 - math.exp(-(ratio ** 3.5))))) * 100, 1)
            anomalies.append({
                'type': 'machine_warning',
                'severity': 'critical' if ratio > 0.95 else 'warning',
                'message': f'{m.machine_name} ({m.machine_id}) at {round(ratio*100)}% of failure threshold — health {health_pct}%',
                'machine_id': m.machine_id,
            })

    # 4. Stale pending deliveries (older than 3 days)
    stale = Delivery.objects.filter(
        status='pending', date__lt=date.today() - timedelta(days=3)
    ).count()
    if stale > 0:
        anomalies.append({
            'type': 'stale_deliveries',
            'severity': 'warning',
            'message': f'{stale} deliveries have been pending for more than 3 days',
        })

    return {
        'period': f'Last {days_back} days',
        'anomalies_found': len(anomalies),
        'anomalies': anomalies,
    }


def _store_delivery(params):
    """Mark a delivery as stored — places all pallets on the assigned shelf."""
    delivery = None

    # Warehouse scoping — only search in operator's current warehouse
    wh_filter = {}
    if v := params.get('warehouse_code'):
        wh_filter['warehouse__code'] = v

    # Try lookup by delivery_id first (most precise)
    if params.get('delivery_id'):
        try:
            delivery = Delivery.objects.get(id=int(params['delivery_id']))
            # Verify warehouse match if specified
            if wh_filter and delivery.warehouse and delivery.warehouse.code != params.get('warehouse_code'):
                delivery = None
        except (Delivery.DoesNotExist, ValueError):
            pass

    # Try by batch_id
    if not delivery and params.get('batch_id'):
        delivery = Delivery.objects.filter(
            batch_id__icontains=params['batch_id'], status='pending', **wh_filter
        ).first()

    # Try by shelf_id
    if not delivery and params.get('shelf_id'):
        qs = Delivery.objects.filter(shelf_id=params['shelf_id'], status='pending', **wh_filter)
        if params.get('manufacturer'):
            qs = qs.filter(manufacturer__icontains=params['manufacturer'])
        if params.get('material'):
            qs = qs.filter(material__name__icontains=params['material'])
        delivery = qs.first()

    # Try by manufacturer + material combo
    if not delivery and (params.get('manufacturer') or params.get('material')):
        qs = Delivery.objects.filter(status='pending', **wh_filter)
        if params.get('manufacturer'):
            qs = qs.filter(manufacturer__icontains=params['manufacturer'])
        if params.get('material'):
            qs = qs.filter(material__name__icontains=params['material'])
        delivery = qs.first()

    if not delivery:
        # Return helpful info about what's available
        pending = Delivery.objects.filter(status='pending', **wh_filter).select_related('material')[:10]
        available = [{'batch_id': d.batch_id, 'manufacturer': d.manufacturer,
                      'material': d.material.name if d.material else None,
                      'shelf_id': d.shelf_id, 'quantity': d.quantity} for d in pending]
        return {
            'error': 'Could not find a matching pending delivery.',
            'search_params': {k: v for k, v in params.items() if v},
            'available_pending': available,
            'hint': 'Try using the batch_id or shelf_id from the list above.',
        }

    if delivery.status == 'stored':
        return {
            'status': 'already_stored',
            'message': f'Delivery {delivery.batch_id} is already stored at shelf {delivery.shelf_id}.',
        }

    shelf_id = delivery.shelf_id
    warehouse = delivery.warehouse

    # Calculate how many pallets needed
    try:
        pallets_needed = int(''.join(c for c in delivery.quantity if c.isdigit()))
    except (ValueError, IndexError):
        pallets_needed = 1

    # Find already stored pallets for this delivery
    already_stored = ShelfSlot.objects.filter(delivery=delivery, is_occupied=True).count()
    remaining = max(0, pallets_needed - already_stored)

    # Store remaining pallets
    stored_slots = []
    for i in range(remaining):
        # Find next available slot index on this shelf
        existing = ShelfSlot.objects.filter(
            shelf_id=shelf_id, warehouse=warehouse
        ).values_list('slot_index', flat=True)
        next_slot = 0
        while next_slot in existing:
            next_slot += 1

        ShelfSlot.objects.update_or_create(
            shelf_id=shelf_id, slot_index=next_slot, warehouse=warehouse,
            defaults={
                'is_occupied': True,
                'delivery': delivery,
                'stored_at': timezone.now(),
            }
        )
        stored_slots.append(next_slot)

    # Mark delivery as stored
    delivery.status = 'stored'
    delivery.save()

    # Log it
    from .views import log_event
    log_event('shipment', 'info', f'Delivery stored: {delivery.batch_id}',
              f'All {pallets_needed} pallets placed on shelf {shelf_id}', delivery=delivery)

    return {
        'status': 'stored',
        'delivery_id': delivery.id,
        'batch_id': delivery.batch_id,
        'manufacturer': delivery.manufacturer,
        'material': delivery.material.name if delivery.material else None,
        'shelf_id': shelf_id,
        'pallets_stored': pallets_needed,
        'message': f'All {pallets_needed} pallets stored on shelf {shelf_id}. Delivery {delivery.batch_id} marked as STORED.',
        'scan_log': [
            {'step': 'lidar_scan', 'result': 'Pallet detected at dock position'},
            {'step': 'position_check', 'result': f'Optimal placement confirmed for shelf {shelf_id}'},
            {'step': 'weight_verify', 'result': f'Weight verified: {delivery.quantity} units'},
            {'step': 'slot_assign', 'result': f'Slots {stored_slots} assigned on shelf {shelf_id}'},
            {'step': 'confirm', 'result': 'Storage confirmed — all sensors green'},
        ],
    }


# ── Maintenance Technician Tool Definitions ─────────────────────────────

MAINTENANCE_TECH_TOOL_DEFINITIONS = [
    {
        "name": "machine_fleet_status",
        "description": "Get a complete overview of all machines: health percentage, usage count, days since last maintenance, recent defect count, and scrap event count. Use this when the user greets you or asks for a general status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "health_below": {"type": "integer", "description": "Only show machines with health below this percentage"},
            },
            "required": [],
        },
    },
    {
        "name": "maintenance_schedule",
        "description": "Get machines sorted by maintenance urgency: overdue (past next_scheduled date), due soon (within 7 days), and never maintained. Helps prioritize which machines to service first.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "defect_correlation",
        "description": "Cross-reference manufacturing defects with machine health data. Groups defects by machine, calculates defect rate vs health percentage, and identifies machines with disproportionate failure rates. Helps find patterns between machine degradation and product quality.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Focus on a specific machine ID"},
                "days_back": {"type": "integer", "description": "Days to analyze (default 30)"},
            },
            "required": [],
        },
    },
    {
        "name": "scrap_analysis",
        "description": "Analyze scrap/waste events by machine: total events, average scrap rate, worst scrap types, and machine rankings by scrap severity. Identifies the biggest waste generators.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Focus on a specific machine ID"},
                "days_back": {"type": "integer", "description": "Days to analyze (default 30)"},
                "scrap_type": {"type": "string", "description": "Filter by scrap type (partial match)"},
            },
            "required": [],
        },
    },
    {
        "name": "machine_history",
        "description": "Get the full timeline for a specific machine: all maintenance entries, defects from manufacturing orders, scrap events, and global log entries. Provides complete machine lifecycle visibility.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Machine ID (e.g. MCH-UL-01)"},
            },
            "required": ["machine_id"],
        },
    },
    {
        "name": "predictive_maintenance",
        "description": "Predict when each machine will need maintenance based on current usage rate. Projects days until failure threshold is reached and estimates maintenance windows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Optional: focus on a specific machine"},
            },
            "required": [],
        },
    },
    {
        "name": "maintenance_shift_report",
        "description": "Generate a maintenance-focused shift handoff report: machine events, defects, scrap events, maintenance performed, and machines that crossed health thresholds in the last N hours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours_back": {"type": "integer", "description": "Hours to look back (default 8, max 24)"},
            },
            "required": [],
        },
    },
    {
        "name": "create_maintenance_log",
        "description": "Create a new maintenance log entry for a machine. Updates the machine's last_maintenance timestamp and logs the event. Use this when the technician wants to record maintenance work.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Machine ID (e.g. MCH-UL-01)"},
                "maintenance_type": {"type": "string", "enum": ["preventive", "corrective", "inspection"], "description": "Type of maintenance"},
                "description": {"type": "string", "description": "What maintenance was performed"},
                "date": {"type": "string", "description": "Date of maintenance (YYYY-MM-DD, defaults to today)"},
                "parts_replaced": {"type": "string", "description": "Parts that were replaced"},
                "technician_notes": {"type": "string", "description": "Additional notes"},
                "next_scheduled": {"type": "string", "description": "Next maintenance date (YYYY-MM-DD)"},
            },
            "required": ["machine_id", "maintenance_type", "description"],
        },
    },
    {
        "name": "order_defect_lookup",
        "description": "Search manufacturing orders focused on defect information. Can filter by defect machine, status, quality. Returns orders with full defect details including machine, type, and cause.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Filter by defect machine ID"},
                "status": {"type": "string", "enum": ["completed", "defected"], "description": "Filter by order status"},
                "quality": {"type": "string", "enum": ["PASS", "FAIL"], "description": "Filter by quality"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
            "required": [],
        },
    },
    {
        "name": "health_trend",
        "description": "Analyze machine health degradation over time by examining manufacturing volume and usage patterns in weekly buckets. Shows how machines are wearing over time.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Optional: focus on a specific machine"},
                "weeks_back": {"type": "integer", "description": "Weeks to analyze (default 8)"},
            },
            "required": [],
        },
    },
    {
        "name": "reset_machine",
        "description": "Reset a machine's usage counter to zero after major maintenance or repair. Updates last_maintenance timestamp and logs the reset event. Use this after the technician confirms they've completed a full maintenance cycle.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Machine ID to reset (e.g. MCH-UL-01)"},
            },
            "required": ["machine_id"],
        },
    },
    {
        "name": "update_failure_threshold",
        "description": "Update the failure threshold for a machine. Use this when inspection reveals the machine can handle more or fewer cycles before needing maintenance. A higher threshold means the machine is more durable; a lower threshold means it needs more frequent servicing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Machine ID (e.g. MCH-UL-01)"},
                "threshold": {"type": "integer", "description": "New failure threshold value (number of uses before failure)"},
            },
            "required": ["machine_id", "threshold"],
        },
    },
    {
        "name": "update_equipment_info",
        "description": "Update specific equipment metadata for a machine: purchase date, depreciation years, wear level, total operating hours, or add a part to the parts changed list. Does NOT replace all data — safely updates individual fields.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Machine ID (e.g. MCH-UL-01)"},
                "purchase_date": {"type": "string", "description": "New purchase date (YYYY-MM-DD)"},
                "depreciation_years": {"type": "integer", "description": "Depreciation period in years"},
                "wear_level": {"type": "integer", "description": "Current wear level percentage (0-100)"},
                "total_hours": {"type": "integer", "description": "Total operating hours"},
                "add_part": {"type": "string", "description": "Add a part to the parts changed list (e.g. 'Belt Assembly (2024)')"},
                "add_resource": {"type": "string", "description": "Add or update a resource name and level, format: 'name:level' (e.g. 'Oil pressure:85')"},
            },
            "required": ["machine_id"],
        },
    },
    {
        "name": "get_equipment_details",
        "description": "Get detailed equipment specifications for a machine: purchase date, depreciation, wear level, total hours, parts changed history, resource levels, and maintenance log from the machine's metadata. Use this to answer specific questions about machine specs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Machine ID (e.g. MCH-UL-01)"},
            },
            "required": ["machine_id"],
        },
    },
    {
        "name": "get_todays_summary",
        "description": "Get a complete summary of everything that happened today: orders processed, defects, scrap events, maintenance performed, machines that need attention, and current fleet health. The ultimate daily overview for a maintenance technician.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_all_maintenance_entries",
        "description": "List all maintenance log entries with full details. Can filter by machine, type, and date range. Returns the complete maintenance history that matches the Maintenance Log UI page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "machine_id": {"type": "string", "description": "Filter by machine ID"},
                "maintenance_type": {"type": "string", "enum": ["preventive", "corrective", "inspection"], "description": "Filter by type"},
                "date_from": {"type": "string", "description": "Start date (YYYY-MM-DD)"},
                "date_to": {"type": "string", "description": "End date (YYYY-MM-DD)"},
                "limit": {"type": "integer", "description": "Max results (default 30)"},
            },
            "required": [],
        },
    },
    {
        "name": "edit_maintenance_log",
        "description": "Edit an existing maintenance log entry. Look up the entry by its ID (from list_all_maintenance_entries) and update any fields. Only the fields you provide will be changed — others stay the same.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "integer", "description": "The maintenance entry ID to edit"},
                "maintenance_type": {"type": "string", "enum": ["preventive", "corrective", "inspection"], "description": "New maintenance type"},
                "description": {"type": "string", "description": "New description of work performed"},
                "date": {"type": "string", "description": "New date (YYYY-MM-DD)"},
                "parts_replaced": {"type": "string", "description": "New parts replaced text"},
                "technician_notes": {"type": "string", "description": "New technician notes"},
                "next_scheduled": {"type": "string", "description": "New next scheduled date (YYYY-MM-DD), pass 'clear' to remove"},
            },
            "required": ["entry_id"],
        },
    },
    {
        "name": "delete_maintenance_log",
        "description": "Delete a maintenance log entry by its ID. Use this when the technician says an entry was logged incorrectly and wants it removed entirely.",
        "input_schema": {
            "type": "object",
            "properties": {
                "entry_id": {"type": "integer", "description": "The maintenance entry ID to delete"},
            },
            "required": ["entry_id"],
        },
    },
]


# ── Maintenance Technician Tool Execution ──────────────────────────────

def _machine_fleet_status(params):
    machines = MachineHealth.objects.all()
    today = date.today()
    rows = []
    for m in machines:
        health = _compute_health(m.usage_count, m.failure_threshold)
        if 'health_below' in params and health >= params['health_below']:
            continue
        days_since = (today - m.last_maintenance.date()).days if m.last_maintenance else None
        defect_count = ManufacturingOrder.objects.filter(
            defect_machine_id=m.machine_id, status='defected'
        ).count()
        scrap_count = ScrapEvent.objects.filter(machine_id=m.machine_id).count()
        last_entry = MaintenanceEntry.objects.filter(machine=m).first()
        rows.append({
            'machine_id': m.machine_id,
            'machine_name': m.machine_name,
            'health_pct': health,
            'usage_count': m.usage_count,
            'failure_threshold': m.failure_threshold,
            'days_since_maintenance': days_since,
            'last_maintenance_entry': str(last_entry.date) if last_entry else None,
            'defect_count': defect_count,
            'scrap_count': scrap_count,
            'position': m.position,
        })
    rows.sort(key=lambda x: x['health_pct'])
    return {'total_machines': len(rows), 'machines': rows}


def _maintenance_schedule(params):
    today = date.today()
    machines = MachineHealth.objects.all()
    overdue = []
    due_soon = []
    never_maintained = []
    healthy = []

    for m in machines:
        health = _compute_health(m.usage_count, m.failure_threshold)
        last_entry = MaintenanceEntry.objects.filter(machine=m).first()
        info = {
            'machine_id': m.machine_id,
            'machine_name': m.machine_name,
            'health_pct': health,
            'last_maintenance_date': str(last_entry.date) if last_entry else None,
            'last_maintenance_type': last_entry.maintenance_type if last_entry else None,
        }

        if last_entry and last_entry.next_scheduled:
            info['next_scheduled'] = str(last_entry.next_scheduled)
            days_until = (last_entry.next_scheduled - today).days
            info['days_until_due'] = days_until
            if days_until < 0:
                info['status'] = 'overdue'
                info['days_overdue'] = abs(days_until)
                overdue.append(info)
            elif days_until <= 7:
                info['status'] = 'due_soon'
                due_soon.append(info)
            else:
                info['status'] = 'scheduled'
                healthy.append(info)
        elif not last_entry:
            info['status'] = 'never_maintained'
            never_maintained.append(info)
        else:
            info['status'] = 'no_next_scheduled'
            healthy.append(info)

    overdue.sort(key=lambda x: x.get('days_overdue', 0), reverse=True)
    return {
        'overdue': overdue,
        'due_soon': due_soon,
        'never_maintained': never_maintained,
        'scheduled': healthy,
        'summary': {
            'overdue_count': len(overdue),
            'due_soon_count': len(due_soon),
            'never_maintained_count': len(never_maintained),
        },
    }


def _defect_correlation(params):
    days_back = min(params.get('days_back', 30), 90)
    cutoff = date.today() - timedelta(days=days_back)
    machines = MachineHealth.objects.all()
    if v := params.get('machine_id'):
        machines = machines.filter(machine_id=v)

    correlations = []
    for m in machines:
        health = _compute_health(m.usage_count, m.failure_threshold)
        total_orders = ManufacturingOrder.objects.filter(created_at__date__gte=cutoff).count()
        defects = ManufacturingOrder.objects.filter(
            defect_machine_id=m.machine_id, status='defected',
            created_at__date__gte=cutoff
        )
        defect_count = defects.count()
        defect_types = list(defects.values('defect_type').annotate(
            count=Count('id')
        ).order_by('-count')[:5])

        correlations.append({
            'machine_id': m.machine_id,
            'machine_name': m.machine_name,
            'health_pct': health,
            'usage_ratio': round(m.usage_count / max(m.failure_threshold, 1) * 100, 1),
            'defect_count': defect_count,
            'defect_rate_pct': round(defect_count / max(total_orders, 1) * 100, 2),
            'top_defect_types': [{'type': d['defect_type'], 'count': d['count']} for d in defect_types],
        })

    correlations.sort(key=lambda x: x['defect_count'], reverse=True)
    return {
        'period': f'Last {days_back} days',
        'total_orders_in_period': ManufacturingOrder.objects.filter(created_at__date__gte=cutoff).count(),
        'correlations': correlations,
    }


def _scrap_analysis(params):
    days_back = min(params.get('days_back', 30), 90)
    cutoff = date.today() - timedelta(days=days_back)
    qs = ScrapEvent.objects.filter(created_at__date__gte=cutoff)
    if v := params.get('machine_id'):
        qs = qs.filter(machine_id=v)
    if v := params.get('scrap_type'):
        qs = qs.filter(scrap_type__icontains=v)

    by_machine = defaultdict(lambda: {'events': 0, 'total_rate': 0, 'types': defaultdict(int)})
    for s in qs:
        entry = by_machine[s.machine_id]
        entry['machine_name'] = s.machine_name
        entry['events'] += 1
        entry['total_rate'] += s.scrap_rate
        entry['types'][s.scrap_type] += 1

    rankings = []
    for mid, data in by_machine.items():
        avg_rate = round(data['total_rate'] / max(data['events'], 1), 2)
        top_types = sorted(data['types'].items(), key=lambda x: x[1], reverse=True)[:3]
        rankings.append({
            'machine_id': mid,
            'machine_name': data['machine_name'],
            'total_scrap_events': data['events'],
            'avg_scrap_rate_pct': avg_rate,
            'top_scrap_types': [{'type': t, 'count': c} for t, c in top_types],
        })

    rankings.sort(key=lambda x: x['total_scrap_events'], reverse=True)
    return {
        'period': f'Last {days_back} days',
        'total_events': qs.count(),
        'machine_rankings': rankings,
    }


def _machine_history(params):
    machine_id = params.get('machine_id', '')
    try:
        m = MachineHealth.objects.get(machine_id=machine_id)
    except MachineHealth.DoesNotExist:
        return {'error': f'Machine {machine_id} not found'}

    health = _compute_health(m.usage_count, m.failure_threshold)

    # Maintenance entries
    entries = MaintenanceEntry.objects.filter(machine=m).order_by('-date')[:20]
    maintenance_list = [{
        'date': str(e.date),
        'type': e.maintenance_type,
        'description': e.description[:200],
        'parts_replaced': e.parts_replaced or None,
        'next_scheduled': str(e.next_scheduled) if e.next_scheduled else None,
    } for e in entries]

    # Defects
    defects = ManufacturingOrder.objects.filter(
        defect_machine_id=machine_id, status='defected'
    ).order_by('-created_at')[:20]
    defect_list = [{
        'order_id': o.order_id,
        'product': o.product,
        'defect_type': o.defect_type,
        'defect_cause': o.defect_cause,
        'created_at': o.created_at,
    } for o in defects]

    # Scrap events
    scraps = ScrapEvent.objects.filter(machine_id=machine_id).order_by('-created_at')[:20]
    scrap_list = [{
        'order_id': s.order.order_id,
        'scrap_type': s.scrap_type,
        'scrap_rate_pct': s.scrap_rate,
        'material': s.material_name or None,
        'created_at': s.created_at,
    } for s in scraps]

    # Logs
    logs = GlobalLog.objects.filter(machine=m).order_by('-timestamp')[:15]
    log_list = [{
        'timestamp': l.timestamp,
        'event_type': l.event_type,
        'severity': l.severity,
        'title': l.title,
    } for l in logs]

    return {
        'machine_id': m.machine_id,
        'machine_name': m.machine_name,
        'health_pct': health,
        'usage_count': m.usage_count,
        'failure_threshold': m.failure_threshold,
        'last_maintenance': m.last_maintenance,
        'detail_data': m.detail_data if m.detail_data else None,
        'maintenance_entries': maintenance_list,
        'defects': defect_list,
        'scrap_events': scrap_list,
        'event_logs': log_list,
    }


def _predictive_maintenance(params):
    machines = MachineHealth.objects.all()
    if v := params.get('machine_id'):
        machines = machines.filter(machine_id=v)

    # Estimate daily usage rate from recent manufacturing orders
    lookback = date.today() - timedelta(days=14)
    total_orders_14d = ManufacturingOrder.objects.filter(created_at__date__gte=lookback).count()
    avg_daily_orders = total_orders_14d / 14.0

    predictions = []
    for m in machines:
        health = _compute_health(m.usage_count, m.failure_threshold)
        remaining = max(0, m.failure_threshold - m.usage_count)
        # Each order typically uses each machine once
        daily_usage_estimate = max(avg_daily_orders, 0.1)
        days_to_threshold = round(remaining / daily_usage_estimate) if daily_usage_estimate > 0 else 999

        predictions.append({
            'machine_id': m.machine_id,
            'machine_name': m.machine_name,
            'health_pct': health,
            'usage_count': m.usage_count,
            'failure_threshold': m.failure_threshold,
            'remaining_uses': remaining,
            'est_daily_usage': round(daily_usage_estimate, 1),
            'est_days_to_threshold': days_to_threshold,
            'est_threshold_date': (date.today() + timedelta(days=days_to_threshold)).isoformat() if days_to_threshold < 999 else 'N/A',
            'urgency': 'critical' if days_to_threshold < 7 else 'soon' if days_to_threshold < 30 else 'ok',
        })

    predictions.sort(key=lambda x: x['est_days_to_threshold'])
    return {
        'avg_daily_production_rate': round(avg_daily_orders, 1),
        'predictions': predictions,
    }


def _maintenance_shift_report(params):
    hours_back = min(params.get('hours_back', 8), 24)
    cutoff = timezone.now() - timedelta(hours=hours_back)

    # Machine events
    machine_logs = GlobalLog.objects.filter(
        timestamp__gte=cutoff,
        event_type__in=['machine', 'threshold', 'scrap', 'manufacturing']
    ).order_by('-timestamp')[:30]

    events = [{
        'timestamp': l.timestamp.isoformat(),
        'event_type': l.event_type,
        'severity': l.severity,
        'title': l.title,
        'machine_id': l.machine.machine_id if l.machine else None,
    } for l in machine_logs]

    # Defects in period
    defects = ManufacturingOrder.objects.filter(
        status='defected', created_at__gte=cutoff
    )
    defect_summary = [{
        'order_id': d.order_id,
        'defect_machine': d.defect_machine,
        'defect_machine_id': d.defect_machine_id,
        'defect_type': d.defect_type,
    } for d in defects[:10]]

    # Scrap in period
    scrap_count = ScrapEvent.objects.filter(created_at__gte=cutoff).count()

    # Maintenance performed
    maintenance_done = MaintenanceEntry.objects.filter(
        created_at__gte=cutoff
    ).select_related('machine')[:10]
    maint_list = [{
        'machine': e.machine.machine_name,
        'type': e.maintenance_type,
        'description': e.description[:100],
    } for e in maintenance_done]

    # Threshold warnings
    threshold_events = GlobalLog.objects.filter(
        timestamp__gte=cutoff, event_type='threshold'
    ).count()

    return {
        'period': f'Last {hours_back} hours',
        'total_events': len(events),
        'events': events,
        'defects_in_period': len(defect_summary),
        'defect_details': defect_summary,
        'scrap_events_in_period': scrap_count,
        'maintenance_performed': maint_list,
        'threshold_crossings': threshold_events,
    }


def _create_maintenance_log(params):
    machine_id = (params.get('machine_id') or '').strip()
    maintenance_type = (params.get('maintenance_type') or '').strip()
    description = (params.get('description') or '').strip()
    if not machine_id or not maintenance_type or not description:
        return {'error': 'machine_id, maintenance_type, and description are required'}
    try:
        machine = MachineHealth.objects.get(machine_id=machine_id)
    except MachineHealth.DoesNotExist:
        return {'error': f'Machine {machine_id} not found'}

    entry_date = _parse_date(params.get('date')) or date.today()
    next_sched = _parse_date(params.get('next_scheduled'))
    entry = MaintenanceEntry.objects.create(
        machine=machine,
        date=entry_date,
        maintenance_type=maintenance_type,
        description=description,
        parts_replaced=(params.get('parts_replaced') or ''),
        technician_notes=(params.get('technician_notes') or ''),
        next_scheduled=next_sched,
    )
    machine.last_maintenance = timezone.now()
    machine.save(update_fields=['last_maintenance'])
    GlobalLog.objects.create(
        event_type='machine',
        severity='info',
        title=f'Maintenance logged: {maintenance_type} on {machine.machine_name}',
        description=description,
        machine=machine,
    )
    return {
        'success': True,
        'entry_id': entry.id,
        'machine_name': machine.machine_name,
        'machine_id': machine.machine_id,
        'date': str(entry.date),
        'type': entry.maintenance_type,
        'message': f'Maintenance entry created for {machine.machine_name}',
    }


def _order_defect_lookup(params):
    qs = ManufacturingOrder.objects.all()
    if v := params.get('machine_id'):
        qs = qs.filter(defect_machine_id=v)
    if v := params.get('status'):
        qs = qs.filter(status=v)
    if v := params.get('quality'):
        qs = qs.filter(quality=v)
    limit = min(params.get('limit', 20), 50)
    rows = []
    for o in qs[:limit]:
        rows.append({
            'order_id': o.order_id,
            'product': o.product,
            'status': o.status,
            'quality': o.quality,
            'defect_machine': o.defect_machine or None,
            'defect_machine_id': o.defect_machine_id or None,
            'defect_type': o.defect_type or None,
            'defect_cause': o.defect_cause or None,
            'stages_completed': o.stages_completed,
            'total_scrap_pct': o.total_scrap,
            'processing_time_sec': o.processing_time,
            'total_energy_kwh': o.total_energy,
            'material': o.material_name or None,
            'created_at': o.created_at,
        })
    return {'count': qs.count(), 'results': rows}


def _health_trend(params):
    weeks_back = min(params.get('weeks_back', 8), 20)
    machines = MachineHealth.objects.all()
    if v := params.get('machine_id'):
        machines = machines.filter(machine_id=v)

    today = date.today()
    trends = []
    for m in machines:
        weekly = []
        for w in range(weeks_back, 0, -1):
            week_start = today - timedelta(weeks=w)
            week_end = today - timedelta(weeks=w - 1)
            orders_in_week = ManufacturingOrder.objects.filter(
                created_at__date__gte=week_start,
                created_at__date__lt=week_end,
            ).count()
            defects_in_week = ManufacturingOrder.objects.filter(
                defect_machine_id=m.machine_id,
                status='defected',
                created_at__date__gte=week_start,
                created_at__date__lt=week_end,
            ).count()
            scrap_in_week = ScrapEvent.objects.filter(
                machine_id=m.machine_id,
                created_at__date__gte=week_start,
                created_at__date__lt=week_end,
            ).count()
            weekly.append({
                'week_start': week_start.isoformat(),
                'orders_processed': orders_in_week,
                'defects': defects_in_week,
                'scrap_events': scrap_in_week,
            })
        trends.append({
            'machine_id': m.machine_id,
            'machine_name': m.machine_name,
            'current_health_pct': _compute_health(m.usage_count, m.failure_threshold),
            'weekly_data': weekly,
        })

    return {'weeks_analyzed': weeks_back, 'trends': trends}


def _reset_machine(params):
    machine_id = (params.get('machine_id') or '').strip()
    if not machine_id:
        return {'error': 'machine_id is required'}
    try:
        m = MachineHealth.objects.get(machine_id=machine_id)
    except MachineHealth.DoesNotExist:
        return {'error': f'Machine {machine_id} not found'}

    old_usage = m.usage_count
    old_health = _compute_health(m.usage_count, m.failure_threshold)
    m.usage_count = 0
    m.last_maintenance = timezone.now()
    m.save()
    GlobalLog.objects.create(
        event_type='machine', severity='info',
        title=f'Machine reset: {m.machine_name} ({machine_id})',
        description=f'Usage counter reset from {old_usage} to 0. Maintenance timestamp updated.',
        machine=m,
    )
    return {
        'success': True,
        'machine_id': m.machine_id,
        'machine_name': m.machine_name,
        'previous_usage': old_usage,
        'previous_health_pct': old_health,
        'new_usage': 0,
        'new_health_pct': 100.0,
        'failure_threshold': m.failure_threshold,
        'message': f'{m.machine_name} has been reset. Usage counter cleared to 0, health restored to 100%.',
    }


def _update_failure_threshold(params):
    machine_id = (params.get('machine_id') or '').strip()
    threshold = params.get('threshold')
    if not machine_id or threshold is None:
        return {'error': 'machine_id and threshold are required'}
    try:
        threshold = int(threshold)
    except (ValueError, TypeError):
        return {'error': 'threshold must be an integer'}
    if threshold < 1:
        return {'error': 'threshold must be at least 1'}
    try:
        m = MachineHealth.objects.get(machine_id=machine_id)
    except MachineHealth.DoesNotExist:
        return {'error': f'Machine {machine_id} not found'}

    old_threshold = m.failure_threshold
    old_health = _compute_health(m.usage_count, m.failure_threshold)
    m.failure_threshold = threshold
    m.save(update_fields=['failure_threshold'])
    new_health = _compute_health(m.usage_count, m.failure_threshold)
    GlobalLog.objects.create(
        event_type='threshold', severity='info',
        title=f'Threshold updated: {m.machine_name}',
        description=f'Failure threshold changed from {old_threshold} to {threshold}. Health went from {old_health}% to {new_health}%.',
        machine=m,
    )
    return {
        'success': True,
        'machine_id': m.machine_id,
        'machine_name': m.machine_name,
        'previous_threshold': old_threshold,
        'new_threshold': threshold,
        'usage_count': m.usage_count,
        'previous_health_pct': old_health,
        'new_health_pct': new_health,
        'message': f'Threshold for {m.machine_name} updated from {old_threshold} to {threshold}. Health is now {new_health}%.',
    }


def _update_equipment_info(params):
    machine_id = (params.get('machine_id') or '').strip()
    if not machine_id:
        return {'error': 'machine_id is required'}
    try:
        m = MachineHealth.objects.get(machine_id=machine_id)
    except MachineHealth.DoesNotExist:
        return {'error': f'Machine {machine_id} not found'}

    detail = m.detail_data or {}
    equipment = detail.get('equipment', {})
    changes = []

    if v := params.get('purchase_date'):
        equipment['purchaseDate'] = v
        changes.append(f'purchase date → {v}')
    if v := params.get('depreciation_years'):
        equipment['depreciationYears'] = int(v)
        changes.append(f'depreciation → {v} years')
    if v := params.get('wear_level'):
        equipment['wearLevel'] = int(v)
        changes.append(f'wear level → {v}%')
    if v := params.get('total_hours'):
        equipment['totalHours'] = int(v)
        changes.append(f'total hours → {v}')

    # Handle legacy format where fields are at top level of detail_data
    if not equipment and any(k in detail for k in ('purchaseDate', 'depreciationYears', 'wearLevel', 'totalHours')):
        if v := params.get('purchase_date'):
            detail['purchaseDate'] = v
        if v := params.get('depreciation_years'):
            detail['depreciationYears'] = int(v)
        if v := params.get('wear_level'):
            detail['wearLevel'] = int(v)
        if v := params.get('total_hours'):
            detail['totalHours'] = int(v)
    else:
        detail['equipment'] = equipment

    if v := params.get('add_part'):
        parts = detail.get('partsChanged', [])
        parts.append(v)
        detail['partsChanged'] = parts
        changes.append(f'added part: {v}')

    if v := params.get('add_resource'):
        if ':' in v:
            name, level = v.rsplit(':', 1)
            resources = detail.get('resources', [])
            found = False
            for r in resources:
                if r.get('name', '').lower() == name.strip().lower():
                    r['level'] = int(level)
                    found = True
                    break
            if not found:
                resources.append({'name': name.strip(), 'level': int(level), 'icon': 'gauge'})
            detail['resources'] = resources
            changes.append(f'resource {name.strip()} → {level}')

    if not changes:
        return {'error': 'No fields to update. Provide at least one field to change.'}

    m.detail_data = detail
    m.save(update_fields=['detail_data'])
    GlobalLog.objects.create(
        event_type='machine', severity='info',
        title=f'Equipment info updated: {m.machine_name}',
        description=f'Updated: {", ".join(changes)}',
        machine=m,
    )
    return {
        'success': True,
        'machine_id': m.machine_id,
        'machine_name': m.machine_name,
        'changes': changes,
        'message': f'Updated equipment info for {m.machine_name}: {", ".join(changes)}',
    }


def _get_equipment_details(params):
    machine_id = params.get('machine_id', '').strip()
    if not machine_id:
        return {'error': 'machine_id is required'}
    try:
        m = MachineHealth.objects.get(machine_id=machine_id)
    except MachineHealth.DoesNotExist:
        return {'error': f'Machine {machine_id} not found'}

    detail = m.detail_data or {}
    health = _compute_health(m.usage_count, m.failure_threshold)

    # Normalize: equipment info may be at top level or nested under 'equipment'
    equipment = detail.get('equipment', {})
    result = {
        'machine_id': m.machine_id,
        'machine_name': m.machine_name,
        'health_pct': health,
        'usage_count': m.usage_count,
        'failure_threshold': m.failure_threshold,
        'last_maintenance': m.last_maintenance,
        'purchase_date': equipment.get('purchaseDate') or detail.get('purchaseDate'),
        'depreciation_years': equipment.get('depreciationYears') or detail.get('depreciationYears'),
        'wear_level': equipment.get('wearLevel') or detail.get('wearLevel'),
        'total_hours': equipment.get('totalHours') or detail.get('totalHours'),
        'parts_changed': detail.get('partsChanged', []),
        'resources': detail.get('resources', []),
        'maintenance_log_from_metadata': detail.get('maintenanceLog', []),
    }

    # Also get structured maintenance entries
    entries = MaintenanceEntry.objects.filter(machine=m).order_by('-date')[:10]
    result['maintenance_entries'] = [{
        'date': str(e.date),
        'type': e.maintenance_type,
        'description': e.description[:200],
        'parts_replaced': e.parts_replaced or None,
        'next_scheduled': str(e.next_scheduled) if e.next_scheduled else None,
    } for e in entries]

    return result


def _get_todays_summary(params):
    today = date.today()
    now = timezone.now()

    # Orders today
    orders_today = ManufacturingOrder.objects.filter(created_at__date=today)
    completed = orders_today.filter(status='completed').count()
    defected = orders_today.filter(status='defected').count()
    passed = orders_today.filter(quality='PASS').count()
    failed = orders_today.filter(quality='FAIL').count()

    # Scrap today
    scrap_today = ScrapEvent.objects.filter(created_at__date=today)
    scrap_count = scrap_today.count()
    avg_scrap_rate = 0
    if scrap_count > 0:
        total_rate = sum(s.scrap_rate for s in scrap_today)
        avg_scrap_rate = round(total_rate / scrap_count, 2)

    # Maintenance performed today
    maint_today = MaintenanceEntry.objects.filter(date=today).select_related('machine')
    maint_list = [{
        'machine': e.machine.machine_name,
        'machine_id': e.machine.machine_id,
        'type': e.maintenance_type,
        'description': e.description[:100],
    } for e in maint_today]

    # Fleet health
    machines = MachineHealth.objects.all()
    machine_health = []
    critical_machines = []
    total_health = 0
    for m in machines:
        h = _compute_health(m.usage_count, m.failure_threshold)
        total_health += h
        machine_health.append({
            'machine_id': m.machine_id,
            'machine_name': m.machine_name,
            'health_pct': h,
            'usage': f'{m.usage_count}/{m.failure_threshold}',
        })
        if h < 50:
            critical_machines.append(m.machine_name)
    avg_health = round(total_health / max(len(machine_health), 1), 1)

    # Recent logs today
    logs_today = GlobalLog.objects.filter(
        timestamp__date=today,
        event_type__in=['machine', 'scrap', 'manufacturing', 'threshold']
    )
    warnings = logs_today.filter(severity__in=['warning', 'error', 'critical']).count()

    # Defect details
    defect_details = []
    for o in orders_today.filter(status='defected')[:5]:
        defect_details.append({
            'order_id': o.order_id,
            'defect_machine': o.defect_machine,
            'defect_type': o.defect_type,
            'defect_cause': o.defect_cause[:100] if o.defect_cause else None,
        })

    return {
        'date': today.isoformat(),
        'production': {
            'total_orders': completed + defected,
            'completed': completed,
            'defected': defected,
            'pass_rate': f'{round(passed / max(passed + failed, 1) * 100, 1)}%',
        },
        'scrap': {
            'total_events': scrap_count,
            'avg_scrap_rate_pct': avg_scrap_rate,
        },
        'maintenance_performed_today': maint_list,
        'fleet_health': {
            'avg_health_pct': avg_health,
            'critical_machines': critical_machines,
            'all_machines': machine_health,
        },
        'defect_details': defect_details,
        'alerts_today': warnings,
        'total_log_events_today': logs_today.count(),
    }


def _list_all_maintenance_entries(params):
    qs = MaintenanceEntry.objects.select_related('machine').all()
    if v := params.get('machine_id'):
        qs = qs.filter(machine__machine_id=v)
    if v := params.get('maintenance_type'):
        qs = qs.filter(maintenance_type=v)
    if v := params.get('date_from'):
        qs = qs.filter(date__gte=v)
    if v := params.get('date_to'):
        qs = qs.filter(date__lte=v)
    limit = min(params.get('limit', 30), 100)
    rows = []
    for e in qs[:limit]:
        rows.append({
            'id': e.id,
            'machine_id': e.machine.machine_id,
            'machine_name': e.machine.machine_name,
            'date': str(e.date),
            'type': e.maintenance_type,
            'description': e.description[:150],
            'parts': e.parts_replaced or None,
            'next_scheduled': str(e.next_scheduled) if e.next_scheduled else None,
        })
    return {'count': qs.count(), 'results': rows}


def _edit_maintenance_log(params):
    entry_id = params.get('entry_id')
    if not entry_id:
        return {'error': 'entry_id is required'}
    try:
        entry = MaintenanceEntry.objects.select_related('machine').get(id=int(entry_id))
    except (MaintenanceEntry.DoesNotExist, ValueError, TypeError):
        return {'error': f'Maintenance entry #{entry_id} not found'}

    changes = []
    if v := (params.get('maintenance_type') or '').strip():
        if v in ('preventive', 'corrective', 'inspection'):
            entry.maintenance_type = v
            changes.append(f'type → {v}')
    if v := (params.get('description') or '').strip():
        entry.description = v
        changes.append(f'description updated')
    if v := params.get('date'):
        parsed = _parse_date(v)
        if parsed:
            entry.date = parsed
            changes.append(f'date → {parsed}')
    if 'parts_replaced' in params:
        entry.parts_replaced = (params.get('parts_replaced') or '')
        changes.append(f'parts_replaced updated')
    if 'technician_notes' in params:
        entry.technician_notes = (params.get('technician_notes') or '')
        changes.append(f'technician_notes updated')
    if v := params.get('next_scheduled'):
        if str(v).strip().lower() == 'clear':
            entry.next_scheduled = None
            changes.append('next_scheduled cleared')
        else:
            parsed = _parse_date(v)
            if parsed:
                entry.next_scheduled = parsed
                changes.append(f'next_scheduled → {parsed}')

    if not changes:
        return {'error': 'No valid fields provided to update'}

    entry.save()
    GlobalLog.objects.create(
        event_type='machine',
        severity='info',
        title=f'Maintenance log edited: entry #{entry.id} for {entry.machine.machine_name}',
        description=f'Updated: {", ".join(changes)}',
        machine=entry.machine,
    )
    return {
        'success': True,
        'entry_id': entry.id,
        'machine_name': entry.machine.machine_name,
        'machine_id': entry.machine.machine_id,
        'changes': changes,
        'current_state': {
            'date': str(entry.date),
            'type': entry.maintenance_type,
            'description': entry.description[:150],
            'parts_replaced': entry.parts_replaced or None,
            'technician_notes': entry.technician_notes or None,
            'next_scheduled': str(entry.next_scheduled) if entry.next_scheduled else None,
        },
    }


def _delete_maintenance_log(params):
    entry_id = params.get('entry_id')
    if not entry_id:
        return {'error': 'entry_id is required'}
    try:
        entry = MaintenanceEntry.objects.select_related('machine').get(id=int(entry_id))
    except (MaintenanceEntry.DoesNotExist, ValueError, TypeError):
        return {'error': f'Maintenance entry #{entry_id} not found'}

    machine_name = entry.machine.machine_name
    machine = entry.machine
    entry_info = {
        'id': entry.id,
        'date': str(entry.date),
        'type': entry.maintenance_type,
        'description': entry.description[:100],
    }
    entry.delete()
    GlobalLog.objects.create(
        event_type='machine',
        severity='info',
        title=f'Maintenance log deleted: entry #{entry_info["id"]} for {machine_name}',
        description=f'Removed {entry_info["type"]} entry from {entry_info["date"]}: {entry_info["description"]}',
        machine=machine,
    )
    return {
        'success': True,
        'deleted_entry': entry_info,
        'machine_name': machine_name,
        'message': f'Deleted maintenance entry #{entry_info["id"]} for {machine_name}',
    }


# ── Dispatcher ──────────────────────────────────────────────────────────

_TOOL_MAP = {
    'search_deliveries': _search_deliveries,
    'search_manufacturing_orders': _search_manufacturing_orders,
    'get_machine_health': _get_machine_health,
    'search_materials': _search_materials,
    'get_warehouse_stats': _get_warehouse_stats,
    'search_logs': _search_logs,
    'get_scrap_events': _get_scrap_events,
    'get_dashboard_summary': _get_dashboard_summary,
    'daily_briefing': _daily_briefing,
    'forklift_route_plan': _forklift_route_plan,
    'capacity_forecast': _capacity_forecast,
    'shift_handoff_summary': _shift_handoff_summary,
    'priority_queue': _priority_queue,
    'anomaly_detection': _anomaly_detection,
    'store_delivery': _store_delivery,
    'machine_fleet_status': _machine_fleet_status,
    'maintenance_schedule': _maintenance_schedule,
    'defect_correlation': _defect_correlation,
    'scrap_analysis': _scrap_analysis,
    'machine_history': _machine_history,
    'predictive_maintenance': _predictive_maintenance,
    'maintenance_shift_report': _maintenance_shift_report,
    'create_maintenance_log': _create_maintenance_log,
    'order_defect_lookup': _order_defect_lookup,
    'health_trend': _health_trend,
    'reset_machine': _reset_machine,
    'update_failure_threshold': _update_failure_threshold,
    'update_equipment_info': _update_equipment_info,
    'get_equipment_details': _get_equipment_details,
    'get_todays_summary': _get_todays_summary,
    'list_all_maintenance_entries': _list_all_maintenance_entries,
    'edit_maintenance_log': _edit_maintenance_log,
    'delete_maintenance_log': _delete_maintenance_log,
}

# Tools relevant for the warehouse operator role
WAREHOUSE_OPERATOR_TOOLS = [
    t for t in TOOL_DEFINITIONS
    if t['name'] in ('search_deliveries', 'search_materials', 'get_warehouse_stats', 'search_logs', 'get_dashboard_summary')
] + WAREHOUSE_OPERATOR_TOOL_DEFINITIONS

# Tools relevant for the maintenance technician role
MAINTENANCE_TECH_TOOLS = [
    t for t in TOOL_DEFINITIONS
    if t['name'] in ('get_machine_health', 'search_manufacturing_orders', 'get_scrap_events', 'search_logs', 'get_dashboard_summary')
] + MAINTENANCE_TECH_TOOL_DEFINITIONS


def execute_tool(tool_name: str, tool_input: dict) -> str:
    fn = _TOOL_MAP.get(tool_name)
    if not fn:
        return json.dumps({'error': f'Unknown tool: {tool_name}'})
    try:
        result = fn(tool_input)
        return _to_json(result)
    except Exception as e:
        return json.dumps({'error': str(e)})
