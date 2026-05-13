from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('QuizGame', '0006_quizparticipant_tutorial_completed'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='QuizBundle',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('synced', models.BooleanField(default=False)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('name', models.CharField(max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('creator', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='quiz_bundles', to=settings.AUTH_USER_MODEL)),
                ('questions', models.ManyToManyField(blank=True, related_name='bundles', to='QuizGame.quizquestion')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
