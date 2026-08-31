from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('Assign', '0017_assignparticipant_set_elimination'),
    ]

    operations = [
        migrations.CreateModel(
            name='AssignSetRuntime',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('hub_session_code', models.CharField(blank=True, db_index=True, default='', max_length=16)),
                ('set_number', models.PositiveIntegerField()),
                ('phase', models.CharField(choices=[('active', 'Active'), ('waiting_reveal', 'Waiting for reveal'), ('revealed', 'Revealed'), ('ended', 'Ended')], default='active', max_length=24)),
                ('effective_time_limit', models.PositiveIntegerField()),
                ('current_round_index', models.PositiveIntegerField(default=0)),
                ('round_started_at', models.DateTimeField()),
                ('round_ends_at', models.DateTimeField()),
                ('solved_matches', models.JSONField(blank=True, default=dict)),
                ('round_participant_ids', models.JSONField(blank=True, default=dict)),
                ('evaluated_rounds', models.JSONField(blank=True, default=list)),
                ('revealed_at', models.DateTimeField(blank=True, null=True)),
                ('ended_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('question', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assign_set_runtimes', to='Assign.assignquestion')),
                ('quiz', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='set_runtimes', to='Assign.assignquiz')),
            ],
        ),
        migrations.CreateModel(
            name='AssignRoundParticipantState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('round_index', models.PositiveIntegerField()),
                ('left_item_index', models.PositiveIntegerField(blank=True, null=True)),
                ('user_match', models.JSONField(blank=True, default=dict)),
                ('is_locked', models.BooleanField(default=False)),
                ('locked_at', models.DateTimeField(blank=True, null=True)),
                ('evaluated_at', models.DateTimeField(blank=True, null=True)),
                ('is_correct', models.BooleanField(blank=True, null=True)),
                ('original_right_index', models.PositiveIntegerField(blank=True, null=True)),
                ('elimination_reason', models.CharField(blank=True, default='', max_length=32)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('participant', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assign_round_states', to='Assign.assignparticipant')),
                ('set_runtime', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='participant_round_states', to='Assign.assignsetruntime')),
            ],
        ),
        migrations.AddConstraint(
            model_name='assignsetruntime',
            constraint=models.UniqueConstraint(fields=('quiz', 'hub_session_code', 'set_number'), name='assign_unique_set_runtime'),
        ),
        migrations.AddConstraint(
            model_name='assignroundparticipantstate',
            constraint=models.UniqueConstraint(fields=('set_runtime', 'participant', 'round_index'), name='assign_unique_participant_round_state'),
        ),
    ]
