# Internal document

https://ericssonab70331551vonagepro-my.sharepoint.com/:w:/r/personal/walter_rodriguez_vonage_com/Documents/Sip%20Retry%20Bug%20Report.docx?d=wf66effb47e8a4b96966f63e633bcfc20&csf=1&web=1&e=61zDhE

In order to run this demo, follow these steps: 

1) Download the Python script from this repository (a log is also included) 
```
https://github.com/walter-vonage/vonage_sip_tester 
```

2) If you are testing this in your local Mac, then use Pinggy or similar to get a temporary TCP connection 
```
ssh -p 443 -R0:localhost:5060 tcp@a.pinggy.io 
```

Pinggy will give you a URL like this you can use for 60 minutes 
``
tcp://ztjsk-2a00-23c5-e3f0-2501-c42-b3e2-8d7c-d43c.run.pinggy- free.link:38107   
```

3) Go to your Vonage Dashboard and create a SIP trunk 
 
Add any name for the SIP domain (mine was “wrodriguez-sip-test”) 

Add the URI 
```
Priority: 1 
IP: ztjsk-2a00-23c5-e3f0-2501-c42-b3e2-8d7c-d43c.run.pinggy-free.link:38107 
Timeout: 5000 
Transport: TCP 
```

And add it with the + 

Then you will have to buy a Portugal number to make the test and link to this SIP. 

4) Go back to your Mac and run the Python script (make sure Pinggy is running) 

5) Stop and restart the terminal with the Python script only for each test, keeping Terminal 2 (Pinggy) running throughout: 

# Test 1: should NOT retry per docs, but customer says it does 
python3 vonage_sip_tester.py --code 404 
 
# Test 2 - 603 Decline, also a definitive rejection, also reportedly retried 
python3 vonage_sip_tester.py --code 603 
 
# Test 3 - 503 baseline: this SHOULD trigger failover (expected behaviour) 
python3 vonage_sip_tester.py --code 503 
 

Make one test call to the Portugal number per run. Ctrl+C after ~30 seconds, check the verdict.  

The log file vonage_sip_tester.log accumulates everything across all runs. 
