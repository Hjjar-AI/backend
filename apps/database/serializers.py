from rest_framework import serializers

class RestoreBackupSerializer(serializers.Serializer):
    backup_name = serializers.CharField()
    admin_password = serializers.CharField()


class ClearDatabaseSerializer(serializers.Serializer):
    admin_password = serializers.CharField()