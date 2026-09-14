"""Start the designated demo, wait for its SSM verification, always stop it."""
import json
import os
from pathlib import Path
import subprocess
import time


def aws(*args):
    return json.loads(subprocess.check_output(['aws', *args, '--output', 'json'], text=True))


def main():
    instance = os.environ['RAG_INSTANCE_ID']
    revision = os.environ['GITHUB_SHA']
    try:
        aws('ec2', 'start-instances', '--instance-ids', instance)
        subprocess.run(['aws', 'ec2', 'wait', 'instance-running', '--instance-ids', instance], check=True)
        for _ in range(90):
            info = aws('ssm', 'describe-instance-information', '--filters', f'Key=InstanceIds,Values={instance}')
            if any(item['PingStatus'] == 'Online' for item in info['InstanceInformationList']):
                break
            time.sleep(5)
        else:
            raise RuntimeError('SSM readiness timeout')
        command = aws('ssm', 'send-command', '--instance-ids', instance, '--document-name', os.environ['RAG_SSM_DOCUMENT'], '--parameters', json.dumps({'Revision':[revision]}))['Command']['CommandId']
        for _ in range(150):
            try:
                result = aws('ssm', 'get-command-invocation', '--command-id', command, '--instance-id', instance)
            except subprocess.CalledProcessError:
                time.sleep(10)
                continue
            if result['Status'] not in {'Pending','InProgress','Delayed'}:
                break
            time.sleep(10)
        else:
            raise RuntimeError('Verification timed out')
        # Reports contain evidence only; SSM stdout/stderr is not copied into CI logs.
        subprocess.run(['aws','s3','cp',f"s3://{os.environ['RAG_BUCKET']}/reports/{revision}.json",'cloud-verification.json'],check=True)
        report = json.loads(Path('cloud-verification.json').read_text())
        if result['Status'] != 'Success' or not report['passed'] or report['revision'] != revision:
            raise RuntimeError('Cloud verification failed; inspect the evidence artifact')
        print('Cloud query, replay, backup/restore and recovery verified.')
    finally:
        aws('ec2','stop-instances','--instance-ids',instance)
        subprocess.run(['aws','ec2','wait','instance-stopped','--instance-ids',instance],check=True)
        print('Demo instance stopped.')


if __name__ == '__main__':
    main()
