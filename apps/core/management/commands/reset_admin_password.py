from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
import secrets
import sys

User = get_user_model()


class Command(BaseCommand):
    help = 'Reset admin password and print it (or optional QR code)'

    def add_arguments(self, parser):
        parser.add_argument('--qr', action='store_true', help='Print QR code (requires qrcode)')

    def handle(self, *args, **options):
        user = User.objects.filter(username='admin').first()
        if not user:
            self.stderr.write('Admin user not found')
            return
        new_pass = secrets.token_urlsafe(12)
        user.set_password(new_pass)
        user.must_change_password = True
        user.save()

        sys.stderr.write('Username: admin\n')
        sys.stderr.write(f'New Password: {new_pass}\n')
        sys.stderr.write(
            '(This is a one-time credential. You will be required to '
            'change it on next login.)\n'
        )

        if options['qr']:
            try:
                import qrcode
                qr = qrcode.QRCode(box_size=1, border=2)
                qr.add_data(new_pass)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            except ImportError:
                self.stderr.write('qrcode not installed, skipping QR')