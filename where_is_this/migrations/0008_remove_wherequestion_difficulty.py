from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('where_is_this', '0007_wherequiz_question_order'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='wherequestion',
            name='difficulty',
        ),
    ]
