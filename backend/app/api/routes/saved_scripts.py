from __future__ import annotations
from datetime import datetime
from typing import Annotated, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.security import get_current_user
from app.models.user import User
from app.models.saved_script import SavedScript

router = APIRouter(prefix='/saved-scripts', tags=['saved-scripts'])


class SavedScriptCreate(BaseModel):
    name:         str
    prompt_text:  str
    content_type: str = 'video'


@router.get('/')
async def list_saved_scripts(
    content_type: Optional[str] = None,
    session:      AsyncSession = Depends(get_session),
    current_user: Annotated[User, Depends(get_current_user)] = None,
):
    query = (
        select(SavedScript)
        .where(SavedScript.user_id == current_user.id)
    )
    if content_type:
        query = query.where(SavedScript.content_type == content_type)
    query = query.order_by(SavedScript.last_used_at.desc().nullslast(),
                           SavedScript.created_at.desc())
    result = await session.exec(query)
    scripts = result.all()
    return {
        'scripts': [
            {
                'id':          s.id,
                'name':        s.name,
                'prompt_text': s.prompt_text,
                'content_type': s.content_type,
                'use_count':   s.use_count,
                'created_at':  s.created_at,
                'last_used_at': s.last_used_at,
            }
            for s in scripts
        ]
    }


@router.post('/')
async def create_saved_script(
    payload:      SavedScriptCreate,
    session:      AsyncSession = Depends(get_session),
    current_user: Annotated[User, Depends(get_current_user)] = None,
):
    result = await session.exec(
        select(SavedScript).where(SavedScript.user_id == current_user.id)
    )
    if len(result.all()) >= 20:
        raise HTTPException(
            status_code=400,
            detail='Maximum 20 saved scripts allowed. Delete one to save a new one.'
        )

    content_type = payload.content_type if payload.content_type in ('video', 'caption') else 'video'
    script = SavedScript(
        user_id      = current_user.id,
        name         = payload.name.strip()[:100],
        prompt_text  = payload.prompt_text.strip(),
        content_type = content_type,
    )
    session.add(script)
    await session.commit()
    await session.refresh(script)

    return {
        'id':          script.id,
        'name':        script.name,
        'prompt_text': script.prompt_text,
        'content_type': script.content_type,
        'use_count':   script.use_count,
        'created_at':  script.created_at,
    }


@router.delete('/{script_id}')
async def delete_saved_script(
    script_id:    int,
    session:      AsyncSession = Depends(get_session),
    current_user: Annotated[User, Depends(get_current_user)] = None,
):
    script = await session.get(SavedScript, script_id)
    if not script or script.user_id != current_user.id:
        raise HTTPException(status_code=404, detail='Script not found')
    await session.delete(script)
    await session.commit()
    return {'deleted': True}


@router.post('/{script_id}/use')
async def mark_script_used(
    script_id:    int,
    session:      AsyncSession = Depends(get_session),
    current_user: Annotated[User, Depends(get_current_user)] = None,
):
    script = await session.get(SavedScript, script_id)
    if not script or script.user_id != current_user.id:
        raise HTTPException(status_code=404, detail='Script not found')
    script.use_count   += 1
    script.last_used_at = datetime.utcnow()
    session.add(script)
    await session.commit()
    return {'use_count': script.use_count}
