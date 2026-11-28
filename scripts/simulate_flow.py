"""Compare synthetic fixed-order vs workload-aware route choices."""
import argparse
import json
from pathlib import Path
from app.routing.plan import Plan
from app.routing.simulation import compare

DEMO={'steps':[
    {'id':'start','label':'Входной этап','rooms':['101']},
    {'id':'test-a','label':'Процедура A','rooms':['102','103'],'requires':['start']},
    {'id':'test-b','label':'Процедура B','rooms':['102','103'],'requires':['start']},
    {'id':'end','label':'Итоговый этап','rooms':['101'],'requires':['test-a','test-b']}]}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,help='JSON file with steps; synthetic only')
    parser.add_argument('--patients',type=int,default=100)
    parser.add_argument('--interarrival',type=float,default=120)
    parser.add_argument('--service',type=float,default=300)
    parser.add_argument('--seed',type=int,default=42)
    args=parser.parse_args()
    plan=Plan.model_validate(json.loads(args.plan.read_text()) if args.plan else DEMO)
    print(json.dumps(compare(plan,args.patients,args.interarrival,args.service,args.seed),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
