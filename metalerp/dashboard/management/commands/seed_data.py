from datetime import date
from django.core.management.base import BaseCommand
from dashboard.models import Material, Delivery, ShelfSlot


MATERIALS = [
    ('HR Coil', 'Steel Sheet'),
    ('CR Sheet', 'Steel Sheet'),
    ('GP Sheet', 'Steel Sheet'),
    ('TMT Bar Fe-500', 'Bar & Rod'),
    ('MS Angle', 'Structural'),
    ('HR Plate', 'Steel Plate'),
    ('SS Sheet 304', 'Stainless Steel'),
    ('SS Sheet 316', 'Stainless Steel'),
    ('GI Pipe', 'Pipe & Tube'),
    ('MS Channel', 'Structural'),
    ('Aluminium Sheet', 'Non-Ferrous'),
    ('Copper Rod', 'Non-Ferrous'),
    ('MS Flat Bar', 'Bar & Rod'),
    ('Chequered Plate', 'Steel Plate'),
    ('Wire Rod', 'Bar & Rod'),
]

DELIVERIES = [
    {'manufacturer': 'Tata Steel Ltd.',     'date': '2026-03-25', 'size': '2.5mm x 1250mm',  'batch_id': 'BATCH-TS-0412',   'quantity': '45 MT',  'shelf_id': '1-A-3', 'material_idx': 0},
    {'manufacturer': 'JSW Steel',           'date': '2026-03-25', 'size': '1.2mm x 1000mm',  'batch_id': 'BATCH-JSW-0198',  'quantity': '80 MT',  'shelf_id': '1-B-2', 'material_idx': 1},
    {'manufacturer': 'SAIL',                'date': '2026-03-24', 'size': '12mm dia',         'batch_id': 'BATCH-SAIL-0087', 'quantity': '120 MT', 'shelf_id': '2-A-1', 'material_idx': 3},
    {'manufacturer': 'Hindalco Industries', 'date': '2026-03-24', 'size': '3mm x 1500mm',    'batch_id': 'BATCH-HIN-0234',  'quantity': '30 MT',  'shelf_id': '2-C-4', 'material_idx': 0},
    {'manufacturer': 'Vedanta Resources',   'date': '2026-03-23', 'size': '25kg blocks',      'batch_id': 'BATCH-VED-0056',  'quantity': '60 MT',  'shelf_id': '3-B-2', 'material_idx': 10},
    {'manufacturer': 'Tata Steel Ltd.',     'date': '2026-03-23', 'size': '0.5mm x 1250mm',  'batch_id': 'BATCH-TS-0413',   'quantity': '15 MT',  'shelf_id': '3-D-1', 'material_idx': 2},
    {'manufacturer': 'Jindal Stainless',    'date': '2026-03-22', 'size': '2mm x 1220mm',    'batch_id': 'BATCH-JIN-0321',  'quantity': '25 MT',  'shelf_id': '4-A-5', 'material_idx': 6},
    {'manufacturer': 'Hindalco Industries', 'date': '2026-03-22', 'size': '8mm dia',          'batch_id': 'BATCH-HIN-0235',  'quantity': '10 MT',  'shelf_id': '4-B-3', 'material_idx': 3},
    {'manufacturer': 'JSW Steel',           'date': '2026-03-21', 'size': '50x50x6mm',        'batch_id': 'BATCH-JSW-0199',  'quantity': '40 MT',  'shelf_id': '5-C-3', 'material_idx': 4},
    {'manufacturer': 'SAIL',                'date': '2026-03-21', 'size': '10mm x 2000mm',   'batch_id': 'BATCH-SAIL-0088', 'quantity': '55 MT',  'shelf_id': '5-A-1', 'material_idx': 5},
    {'manufacturer': 'Tata Steel Ltd.',     'date': '2026-03-20', 'size': '1.6mm x 1250mm',  'batch_id': 'BATCH-TS-0414',   'quantity': '35 MT',  'shelf_id': '6-B-1', 'material_idx': 1},
    {'manufacturer': 'Vedanta Resources',   'date': '2026-03-20', 'size': '50kg blocks',      'batch_id': 'BATCH-VED-0057',  'quantity': '20 MT',  'shelf_id': '6-C-2', 'material_idx': 10},
    {'manufacturer': 'JSW Steel',           'date': '2026-03-19', 'size': '0.8mm x 1000mm',  'batch_id': 'BATCH-JSW-0200',  'quantity': '65 MT',  'shelf_id': '7-A-2', 'material_idx': 2},
    {'manufacturer': 'Jindal Stainless',    'date': '2026-03-19', 'size': '1.5mm x 1220mm',  'batch_id': 'BATCH-JIN-0322',  'quantity': '18 MT',  'shelf_id': '7-B-4', 'material_idx': 7},
    {'manufacturer': 'SAIL',                'date': '2026-03-18', 'size': '16mm dia',         'batch_id': 'BATCH-SAIL-0089', 'quantity': '90 MT',  'shelf_id': '1-C-5', 'material_idx': 3},
    {'manufacturer': 'Tata Steel Ltd.',     'date': '2026-03-18', 'size': '6mm x 2000mm',    'batch_id': 'BATCH-TS-0415',   'quantity': '70 MT',  'shelf_id': '2-B-6', 'material_idx': 5},
    {'manufacturer': 'Hindalco Industries', 'date': '2026-03-17', 'size': '4mm x 1500mm',    'batch_id': 'BATCH-HIN-0236',  'quantity': '22 MT',  'shelf_id': '4-D-2', 'material_idx': 0},
    {'manufacturer': 'JSW Steel',           'date': '2026-03-17', 'size': '3.15mm x 1250mm', 'batch_id': 'BATCH-JSW-0201',  'quantity': '50 MT',  'shelf_id': '5-B-1', 'material_idx': 1},
]

# Initial shelf occupancy — capped to slots 0-3 (1 row × 4 cols)
INITIAL_OCCUPANCY = {
    '1-A-1': [0, 1, 2, 3],
    '1-A-2': [0, 1],
    '1-B-3': [0, 2],
    '2-A-1': [0, 1, 2],
    '2-C-4': [0, 1, 2, 3],
    '3-B-2': [0, 3],
    '3-D-1': [0, 1, 2, 3],
    '4-A-5': [0, 1],
    '5-C-3': [0, 1, 2, 3],
    '6-B-1': [0],
    '7-A-2': [0, 1, 2, 3],
}


class Command(BaseCommand):
    help = 'Seed the database with initial materials, deliveries, and shelf occupancy'

    def handle(self, *args, **options):
        # Materials
        material_objs = []
        for name, category in MATERIALS:
            obj, created = Material.objects.get_or_create(name=name, defaults={'category': category})
            material_objs.append(obj)
            if created:
                self.stdout.write(f'  Created material: {obj}')

        # Deliveries
        for d in DELIVERIES:
            mat = material_objs[d['material_idx']]
            obj, created = Delivery.objects.get_or_create(
                batch_id=d['batch_id'],
                defaults={
                    'manufacturer': d['manufacturer'],
                    'date': date.fromisoformat(d['date']),
                    'size': d['size'],
                    'quantity': d['quantity'],
                    'shelf_id': d['shelf_id'],
                    'status': 'pending',
                    'material': mat,
                },
            )
            if created:
                self.stdout.write(f'  Created delivery: {obj}')

        # Shelf occupancy
        for shelf_id, slots in INITIAL_OCCUPANCY.items():
            for slot_idx in slots:
                obj, created = ShelfSlot.objects.get_or_create(
                    shelf_id=shelf_id,
                    slot_index=slot_idx,
                    defaults={'is_occupied': True},
                )
                if created:
                    self.stdout.write(f'  Created slot: {obj}')

        self.stdout.write(self.style.SUCCESS('Seed data loaded successfully.'))
