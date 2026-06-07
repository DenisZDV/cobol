      *================================================================*
      * EMPLOYEE MASTER FILE - COPYBOOK                               *
      * Used by: PAYROLL, HR, BENEFITS systems                        *
      *================================================================*
       01  EMPLOYEE-RECORD.
           05  EMP-ID              PIC 9(6).
           05  EMP-NAME.
               10  EMP-LAST-NAME   PIC X(20).
               10  EMP-FIRST-NAME  PIC X(15).
           05  EMP-DEPARTMENT      PIC X(6).
           05  EMP-HIRE-DATE.
               10  EMP-HIRE-YYYY   PIC 9(4).
               10  EMP-HIRE-MM     PIC 9(2).
               10  EMP-HIRE-DD     PIC 9(2).
           05  EMP-HOURLY-RATE     PIC 9(4)V99.
           05  EMP-HOURS-WORKED    PIC 9(3)V99.
           05  EMP-TAX-CODE        PIC X(2).
           05  EMP-STATUS          PIC X(1).
               88  EMP-ACTIVE      VALUE 'A'.
               88  EMP-INACTIVE    VALUE 'I'.
               88  EMP-ON-LEAVE    VALUE 'L'.
