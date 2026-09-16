B=/private/tmp/claude-501/-Users-zayankhan-projects-polymarket-crypto/6f03044c-3dcc-4df5-b5f5-73f1372f4182/scratchpad/kronos-test; python3 -c "
import pandas as pd
c = pd.read_csv('$B/macro_calendar_unverified.csv')
c = c[c.verified == True]
c.to_csv('$B/macro_calendar.csv', index=False)
print(len(c), c.event.value_counts().to_dict())
"; cd $B && python3 factors.py 2>&1 | grep -E "macro|CPI|FOMC|====" 
