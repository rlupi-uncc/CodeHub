# Generated by Django 5.1.6 on 2025-04-08 02:40

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('problems', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name='problem',
            name='attempted_by',
            field=models.ManyToManyField(blank=True, related_name='attempted_problems', to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(
            model_name='problem',
            name='solved_by',
            field=models.ManyToManyField(blank=True, related_name='solved_problems', to=settings.AUTH_USER_MODEL),
        ),
    ]
