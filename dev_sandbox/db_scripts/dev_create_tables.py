from dependency_injector.wiring import inject, Provide
from toredocore.db_model.BaseType import Base

from pymix.containers import Container
from pymix.registration import register_app


@inject
def create_tables(
        db_engine=Provide[Container.db_engine],
):
    #Base.metadata.drop_all(db_engine)
    Base.metadata.create_all(db_engine)


if __name__ == "__main__":
    app, app_config = register_app('dev')
    app.container.wire(modules=[__name__])
    create_tables()
