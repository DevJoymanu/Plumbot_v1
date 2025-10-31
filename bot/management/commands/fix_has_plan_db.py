from django.core.management.base import BaseCommand
from django.db import connection

class Command(BaseCommand):
    help = 'Fix has_plan database constraint'

    def handle(self, *args, **options):
        self.stdout.write("🔧 Fixing has_plan database constraint...")
        
        try:
            with connection.cursor() as cursor:
                # Drop NOT NULL constraint
                cursor.execute("""
                    ALTER TABLE bot_appointment 
                    ALTER COLUMN has_plan DROP NOT NULL;
                """)
                self.stdout.write("✅ Dropped NOT NULL constraint")
                
                # Set default to NULL
                cursor.execute("""
                    ALTER TABLE bot_appointment 
                    ALTER COLUMN has_plan SET DEFAULT NULL;
                """)
                self.stdout.write("✅ Set default to NULL")
                
                # Update existing records
                cursor.execute("""
                    UPDATE bot_appointment 
                    SET has_plan = NULL 
                    WHERE has_plan = false 
                      AND (customer_area IS NULL OR customer_area = '');
                """)
                rows_updated = cursor.rowcount
                self.stdout.write(f"✅ Updated {rows_updated} existing appointments")
                
                # Verify
                cursor.execute("""
                    SELECT is_nullable, column_default
                    FROM information_schema.columns 
                    WHERE table_name = 'bot_appointment' 
                      AND column_name = 'has_plan';
                """)
                result = cursor.fetchone()
                
                if result and result[0] == 'YES':
                    self.stdout.write("\n🎉 SUCCESS! Database constraint fixed!")
                    self.stdout.write(f"   is_nullable: {result[0]}")
                    self.stdout.write(f"   default: {result[1]}")
                else:
                    self.stdout.write("\n⚠️ WARNING: Constraint might not be fixed correctly")
                    
        except Exception as e:
            self.stdout.write(f"\n❌ ERROR: {str(e)}")