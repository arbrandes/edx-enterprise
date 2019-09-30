# -*- coding: utf-8 -*-
# Generated by Django 1.11.23 on 2019-10-01 07:42
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('sap_success_factors', '0019_auto_20190925_0730'),
    ]

    operations = [
        migrations.AddField(
            model_name='sapsuccessfactorsenterprisecustomerconfiguration',
            name='catalogs_to_transmit',
            field=models.TextField(blank=True, help_text='A comma-separated list of catalog UUIDs to transmit.', null=True),
        ),
    ]
