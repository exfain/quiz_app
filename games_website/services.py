from django.apps import apps
from django.db import models, transaction


def sync_all_models_to_supabase(stdout=None, stderr=None):
    """Sync all models from default DB to the 'supabase' DB.

    Returns (total_synced, synced_models) where:
    - total_synced: total number of upserted records
    - synced_models: list of model names that had records upserted or deleted
    """
    if stdout is None:
        # Fallback no-op writer
        class _Stdout:
            def write(self, msg):
                pass
        stdout = _Stdout()

    if stderr is None:
        class _Stderr:
            def write(self, msg):
                pass
        stderr = _Stderr()

    stdout.write("Starting data sync to Supabase...\n")

    # Discover all concrete, managed models
    all_models = []
    for app_config in apps.get_app_configs():
        for model in app_config.get_models():
            if not model._meta.abstract and model._meta.managed:
                all_models.append(model)

    # Skip some Django internals
    skip_models = {'LogEntry', 'Permission', 'Group', 'ContentType', 'Session'}
    synced_models = []

    total_synced = 0

    for model in all_models:
        model_name = model.__name__
        if model_name in skip_models:
            stdout.write(f"Skipping {model_name}...\n")
            continue

        has_synced_field = hasattr(model, 'synced')
        fields = [f.name for f in model._meta.fields]

        if has_synced_field:
            queryset = model.objects.filter(synced=False)
        else:
            queryset = model.objects.all()

        count = queryset.count()
        stdout.write(f"Syncing {count} items from {model_name}...\n")
        synced_count = 0

        for item in queryset.iterator():
            try:
                with transaction.atomic(using='supabase'):
                    defaults = {}
                    for field in fields:
                        if field in ("id", "synced"):
                            continue

                        field_obj = model._meta.get_field(field)

                        # Skip auto timestamp fields
                        if getattr(field_obj, "auto_now", False) or getattr(field_obj, "auto_now_add", False):
                            continue

                        # For FK / OneToOne fields, sync the raw ID using the *_id convention
                        if isinstance(field_obj, (models.ForeignKey, models.OneToOneField)):
                            defaults[f"{field}_id"] = getattr(item, f"{field}_id")
                        else:
                            defaults[field] = getattr(item, field)

                    if has_synced_field:
                        defaults['synced'] = True

                    model.objects.using('supabase').update_or_create(
                        id=item.id,
                        defaults=defaults,
                    )

                if has_synced_field:
                    # Use queryset.update to avoid triggering model save signals
                    type(item).objects.filter(pk=item.pk).update(synced=True)

                synced_count += 1
            except Exception as e:  # pylint: disable=broad-except
                stderr.write(f"Error syncing {model_name} ID {item.id}: {e}\n")

        stdout.write(f"Successfully synced {synced_count}/{count} items from {model_name}\n")
        total_synced += synced_count

        # Deletion sync: remove rows in Supabase that no longer exist locally
        ids_to_delete = set()
        try:
            local_ids = list(model.objects.values_list("id", flat=True))
            supabase_ids = list(model.objects.using("supabase").values_list("id", flat=True))
            ids_to_delete = set(supabase_ids) - set(local_ids)
            if ids_to_delete:
                stdout.write(
                    f"Deleting {len(ids_to_delete)} items from {model_name} in Supabase that no longer exist locally...\n"
                )
                model.objects.using("supabase").filter(id__in=ids_to_delete).delete()
        except Exception as e:  # pylint: disable=broad-except
            stderr.write(f"Error syncing deletions for {model_name}: {e}\n")

        if synced_count > 0 or ids_to_delete:
            synced_models.append(model_name)

    stdout.write(f"Sync completed. Total items synced: {total_synced}\n")
    return total_synced, synced_models


def restore_all_models_from_supabase(stdout=None, stderr=None, target_alias="default"):
    """Restore all data from the 'supabase' DB into the local SQLite DB.

    This reads every row for every concrete, managed model from the
    'supabase' database and mirrors it into the target_alias (by default
    the local 'default' SQLite DB), using `update_or_create`.

    For each model it also deletes any local rows whose IDs do not exist
    in Supabase, so the local DB mirrors Supabase for those models.

    Returns (total_restored, restored_models).
    """
    if stdout is None:
        class _Stdout:
            def write(self, msg):
                pass
        stdout = _Stdout()

    if stderr is None:
        class _Stderr:
            def write(self, msg):
                pass
        stderr = _Stderr()

    stdout.write("Starting restore from Supabase to local DB...\n")

    # Discover all concrete, managed models
    all_models = []
    for app_config in apps.get_app_configs():
        for model in app_config.get_models():
            if not model._meta.abstract and model._meta.managed:
                all_models.append(model)

    # Skip some Django internals
    skip_models = {"LogEntry", "Permission", "Group", "ContentType", "Session"}

    total_restored = 0
    restored_models: list[str] = []

    for model in all_models:
        model_name = model.__name__
        if model_name in skip_models:
            stdout.write(f"Skipping {model_name}...\n")
            continue

        fields = [f.name for f in model._meta.fields]

        # Load everything from Supabase for this model
        try:
            supabase_qs = model.objects.using("supabase").all()
        except Exception as e:  # pylint: disable=broad-except
            stderr.write(f"Error accessing Supabase for {model_name}: {e}\n")
            continue
        
        count = supabase_qs.count()
        stdout.write(f"Restoring {count} items for {model_name} from Supabase...\n")

        restored_count = 0

        for item in supabase_qs.iterator():
            try:
                with transaction.atomic(using=target_alias):
                    defaults = {}
                    for field in fields:
                        if field in ("id", "synced"):
                            continue

                        field_obj = model._meta.get_field(field)

                        if getattr(field_obj, "auto_now", False) or getattr(field_obj, "auto_now_add", False):
                            continue

                        # For FK / OneToOne fields, sync the raw ID using the *_id convention
                        if isinstance(field_obj, (models.ForeignKey, models.OneToOneField)):
                            defaults[f"{field}_id"] = getattr(item, f"{field}_id")
                        else:
                            defaults[field] = getattr(item, field)

                    # Preserve synced flag if present in schema
                    if hasattr(model, "synced"):
                        defaults["synced"] = getattr(item, "synced", False)

                    model.objects.using(target_alias).update_or_create(
                        id=item.id,
                        defaults=defaults,
                    )

                restored_count += 1
            except Exception as e:  # pylint: disable=broad-except
                stderr.write(f"Error restoring {model_name} ID {item.id}: {e}\n")

        # Deletion sync in local DB: remove rows not present in Supabase
        ids_to_delete = set()
        try:
            supabase_ids = list(model.objects.using("supabase").values_list("id", flat=True))
            local_ids = list(model.objects.using(target_alias).values_list("id", flat=True))
            ids_to_delete = set(local_ids) - set(supabase_ids)
            if ids_to_delete:
                stdout.write(
                    f"Deleting {len(ids_to_delete)} local {model_name} rows that do not exist in Supabase...\n"
                )
                model.objects.using(target_alias).filter(id__in=ids_to_delete).delete()
        except Exception as e:  # pylint: disable=broad-except
            stderr.write(f"Error syncing local deletions for {model_name}: {e}\n")

        stdout.write(f"Restored {restored_count}/{count} items for {model_name}\n")
        total_restored += restored_count

        if restored_count > 0 or ids_to_delete:
            restored_models.append(model_name)

    stdout.write(f"Restore completed. Total items restored: {total_restored}\n")
    return total_restored, restored_models
