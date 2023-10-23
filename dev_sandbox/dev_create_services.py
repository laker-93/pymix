import asyncio

from pymix.orchestrators.services_orchestrator import ServicesOrchestrator
from pymix.registration import create_container


async def create_services(services_orchestrator: ServicesOrchestrator):
    return services_orchestrator.create('lajp', 'lajp')



async def main():
    container = create_container('dev')
    container.wire(modules=[__name__])
    orchestrator = container.services_orchestrator()
    result = await create_services(orchestrator)

if __name__ == "__main__":
    asyncio.run(main())
