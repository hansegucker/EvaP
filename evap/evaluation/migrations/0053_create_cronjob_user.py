# -*- coding: utf-8 -*-
# Generated by Django 1.9.5 on 2016-05-05 14:42
from __future__ import unicode_literals

from django.db import migrations


def create_cronjob_user(apps, schema_editor):
    UserProfile = apps.get_model('evaluation', 'UserProfile')

    UserProfile.objects.create(username="cronjob")


def delete_cronjob_user(apps, schema_editor):
    UserProfile = apps.get_model('evaluation', 'UserProfile')

    UserProfile.objects.get(username="cronjob").delete()


class Migration(migrations.Migration):

    dependencies = [
        ('evaluation', '0052_add_course_is_private'),
    ]

    operations = [
        migrations.RunPython(create_cronjob_user, reverse_code=delete_cronjob_user),
    ]
