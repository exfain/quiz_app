from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('QuizGame', '0009_quiz_question_order'),
    ]

    operations = [
        migrations.AddField(
            model_name='quizquestion',
            name='correct_answer_2',
            field=models.CharField(blank=True, default='', help_text='For double answer: the correct answer text for field 2.', max_length=500),
        ),
        migrations.AddField(
            model_name='quizquestion',
            name='double_answer_label_1',
            field=models.CharField(blank=True, default='', help_text='For double answer: label for field 1.', max_length=120),
        ),
        migrations.AddField(
            model_name='quizquestion',
            name='double_answer_label_2',
            field=models.CharField(blank=True, default='', help_text='For double answer: label for field 2.', max_length=120),
        ),
        migrations.AlterField(
            model_name='quizquestion',
            name='question_type',
            field=models.CharField(choices=[('multiple_choice', 'Multiple Choice'), ('true_false', 'True/False'), ('short_answer', 'Short Answer'), ('double_answer', 'Double Answer')], default='multiple_choice', max_length=20),
        ),
    ]
