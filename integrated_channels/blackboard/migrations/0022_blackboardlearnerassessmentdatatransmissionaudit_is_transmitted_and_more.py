# Generated by Django 4.2.13 on 2024-05-30 06:43

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('blackboard', '0021_auto_20240423_1057'),
    ]

    operations = [
        migrations.AddField(
            model_name='blackboardlearnerassessmentdatatransmissionaudit',
            name='is_transmitted',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='blackboardlearnerdatatransmissionaudit',
            name='is_transmitted',
            field=models.BooleanField(default=False),
        ),
    ]