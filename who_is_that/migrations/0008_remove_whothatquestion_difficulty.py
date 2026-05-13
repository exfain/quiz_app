from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('who_is_that', '0007_whothatquiz_question_order'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='whothatquestion',
            name='difficulty',
        ),
    ]
