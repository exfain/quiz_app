from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('wer_weiss_mehr', '0003_werweissmehrgame_tutorial_question'),
    ]

    operations = [
        migrations.AddField(
            model_name='werweissmehrroundresponse',
            name='time_taken',
            field=models.FloatField(default=0),
        ),
    ]
