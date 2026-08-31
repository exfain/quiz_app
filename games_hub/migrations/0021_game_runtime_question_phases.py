from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('games_hub', '0020_hubgamestep_intro_state'),
    ]

    operations = [
        migrations.AddField(
            model_name='gameruntimestate',
            name='content_revealed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='gameruntimestate',
            name='question_flow_mode',
            field=models.CharField(
                choices=[
                    ('legacy_immediate', 'Legacy immediate'),
                    ('manual_three_phase', 'Manual three phase'),
                ],
                default='legacy_immediate',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='gameruntimestate',
            name='question_phase',
            field=models.CharField(
                blank=True,
                choices=[
                    ('prompt_visible', 'Prompt visible'),
                    ('content_visible', 'Content visible'),
                    ('answering_open', 'Answering open'),
                ],
                default='',
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name='gameruntimestate',
            name='question_presented_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
