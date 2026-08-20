from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0002_sysuser_logintoken"),
    ]

    operations = [
        migrations.AddField(
            model_name="apsarchive",
            name="remark",
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
    ]
